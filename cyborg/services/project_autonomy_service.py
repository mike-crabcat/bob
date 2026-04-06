"""Autonomous task/project progression after task completion."""

from __future__ import annotations

import logging
from typing import Any

from aiosqlite import Connection

from cyborg.database import Database
from cyborg.models import JournalEntryType, ProjectState, TaskStatus
from cyborg.services.base import BaseService, utcnow, json_dumps, json_loads
from cyborg.services.notification_service import NotificationService


logger = logging.getLogger(__name__)

# Lazy import structured logging helpers
_structured_logger = None


def _get_structured_logger():
    """Lazy import structured logging helpers."""
    global _structured_logger
    if _structured_logger is None:
        from cyborg.structured_logging import get_logger as _get_logger
        _structured_logger = _get_logger(__name__)
    return _structured_logger


DEPENDENCY_BLOCKED_PREFIX = "Waiting for dependency task"


class ProjectAutonomyService(BaseService):
    """Release dependency-blocked tasks and checkpoint projects after task completion."""

    def __init__(self, db: Database, execution_service: Any | None = None) -> None:
        super().__init__(db)
        self._execution_service = execution_service
        self._reasoning_service = None

    @property
    def execution_service(self) -> Any:
        if self._execution_service is None:
            from cyborg.services.project_execution_service import ProjectExecutionService

            self._execution_service = ProjectExecutionService(self.db)
        return self._execution_service

    @property
    def reasoning_service(self) -> Any:
        """Lazy-load reasoning service."""
        if self._reasoning_service is None:
            from cyborg.services.openclaw_reasoning_service import OpenClawReasoningService

            self._reasoning_service = OpenClawReasoningService(self.db)
        return self._reasoning_service

    async def on_task_completed(
        self,
        task_id: str,
        task_title: str,
        result_summary: str | None = None,
        enable_refinement: bool = True,
    ) -> None:
        """
        Handle task completion with optional strategy refinement.

        Args:
            task_id: The completed task ID
            task_title: Title of the completed task
            result_summary: Summary of the task result
            enable_refinement: Whether to trigger strategy refinement (default: True)
        """
        await self._release_unblocked_dependents(task_id)
        await self.execution_service.on_task_completed(task_id, task_title, result_summary)

        for project_id in await self._get_project_ids_for_task(task_id):
            await self._checkpoint_project(project_id)

            # Trigger strategy refinement for auto-executing projects
            if enable_refinement:
                await self.checkpoint_and_refine(project_id, task_id)

    async def _release_unblocked_dependents(self, completed_task_id: str) -> None:
        rows = await self.db.fetch_all(
            """
            SELECT *
            FROM tasks
            WHERE parent_id = ? AND deleted_at IS NULL
            ORDER BY created_at ASC
            """,
            (completed_task_id,),
        )
        if not rows:
            return

        now = utcnow().isoformat()
        released_task_ids: list[str] = []
        async with self.db.connection(write=True) as connection:
            for row in rows:
                if row["status"] in {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value}:
                    continue
                if not await self._dependency_is_satisfied_connection(connection, row):
                    continue

                next_status = await self._released_status(connection, row["id"])
                updates: list[str] = ["status = ?", "updated_at = ?"]
                params: list[Any] = [next_status.value, now]
                if next_status == TaskStatus.ACTIVE:
                    updates.append("started_at = COALESCE(started_at, ?)")
                    params.append(now)
                if row.get("blocked_reason", "").startswith(DEPENDENCY_BLOCKED_PREFIX):
                    updates.append("blocked_reason = NULL")
                    updates.append("blocked_resume_instructions = NULL")
                await connection.execute(
                    f"UPDATE tasks SET {', '.join(updates)} WHERE id = ? AND deleted_at IS NULL",
                    tuple(params + [row["id"]]),
                )
                await self._add_history(
                    connection,
                    row["id"],
                    "dependency_released",
                    {"status": next_status.value, "released_by": completed_task_id},
                    now,
                )
                # Propagate parent task files to dependent metadata
                await self._propagate_dependency_files(connection, completed_task_id, row["id"])
                released_task_ids.append(row["id"])

        notification_service = NotificationService(self.db)
        for task_id in released_task_ids:
            await notification_service.sync_task_state(task_id, immediate=True)

    async def _propagate_dependency_files(
        self,
        connection: Connection,
        parent_task_id: str,
        dependent_task_id: str,
    ) -> None:
        """Store parent task file references in dependent task's metadata."""
        # Fetch parent task files
        cursor = await connection.execute(
            "SELECT task_id, filename, relative_path, purpose FROM task_files WHERE task_id = ?",
            (parent_task_id,),
        )
        file_rows = await cursor.fetchall()
        await cursor.close()

        if not file_rows:
            return

        # Build dependency_output_files list
        dependency_files = [
            {
                "task_id": row["task_id"],
                "filename": row["filename"],
                "relative_path": row["relative_path"],
                "purpose": row["purpose"],
            }
            for row in file_rows
        ]

        # Merge into existing metadata
        dep_row = await self._fetch_one_connection(
            connection,
            "SELECT metadata FROM tasks WHERE id = ?",
            (dependent_task_id,),
        )
        if dep_row is None:
            return

        metadata = json_loads(dep_row.get("metadata"), {})
        metadata["dependency_output_files"] = dependency_files
        await connection.execute(
            "UPDATE tasks SET metadata = ? WHERE id = ?",
            (json_dumps(metadata), dependent_task_id),
        )

    async def _checkpoint_project(self, project_id: str) -> None:
        project = await self.db.fetch_one(
            """
            SELECT id, state, auto_execute
            FROM projects
            WHERE id = ? AND deleted_at IS NULL
            """,
            (project_id,),
        )
        if project is None:
            return
        if project["state"] != ProjectState.ACTIVE.value or not bool(project.get("auto_execute", 0)):
            return
        if await self._project_has_incomplete_tasks(project_id):
            return
        await self.execution_service.evaluate_and_complete(project_id)

    async def _project_has_incomplete_tasks(self, project_id: str) -> bool:
        row = await self.db.fetch_one(
            """
            SELECT 1 AS has_open
            FROM tasks AS t
            INNER JOIN project_tasks AS pt ON pt.task_id = t.id
            WHERE pt.project_id = ?
              AND t.deleted_at IS NULL
              AND t.status IN (?, ?, ?)
            LIMIT 1
            """,
            (
                project_id,
                TaskStatus.BLOCKED.value,
                TaskStatus.PENDING.value,
                TaskStatus.ACTIVE.value,
            ),
        )
        return row is not None

    async def _get_project_ids_for_task(self, task_id: str) -> list[str]:
        rows = await self.db.fetch_all(
            """
            SELECT pt.project_id
            FROM project_tasks AS pt
            INNER JOIN projects AS p ON p.id = pt.project_id
            WHERE pt.task_id = ? AND p.deleted_at IS NULL
            ORDER BY pt.project_id
            """,
            (task_id,),
        )
        return [row["project_id"] for row in rows]

    async def _released_status(self, connection: Connection, task_id: str) -> TaskStatus:
        """Determine the status for a dependency-released task.

        Returns ACTIVE if the task belongs to an auto-executing project,
        PENDING otherwise.
        """
        project_row = await self._fetch_one_connection(
            connection,
            """
            SELECT p.auto_execute
            FROM projects AS p
            INNER JOIN project_tasks AS pt ON pt.project_id = p.id
            WHERE pt.task_id = ? AND p.deleted_at IS NULL
            LIMIT 1
            """,
            (task_id,),
        )
        if project_row and bool(project_row.get("auto_execute", 0)):
            return TaskStatus.ACTIVE
        return TaskStatus.PENDING

    async def _dependency_is_satisfied_connection(self, connection: Connection, row: dict[str, Any]) -> bool:
        parent_id = row.get("parent_id")
        if not parent_id:
            return True
        parent = await self._fetch_one_connection(
            connection,
            "SELECT status, deleted_at FROM tasks WHERE id = ?",
            (parent_id,),
        )
        if parent is None or parent.get("deleted_at") is not None:
            return True
        return parent["status"] == TaskStatus.COMPLETED.value

    async def _add_history(
        self,
        connection: Connection,
        task_id: str,
        action: str,
        details: dict[str, Any],
        timestamp: str,
    ) -> None:
        from uuid import uuid4
        from cyborg.services.base import json_dumps

        await connection.execute(
            "INSERT INTO task_history (id, task_id, action, details, timestamp) VALUES (?, ?, ?, ?, ?)",
            (str(uuid4()), task_id, action, json_dumps(details), timestamp),
        )

    async def _fetch_one_connection(
        self,
        connection: Connection,
        query: str,
        params: tuple[Any, ...],
    ) -> dict[str, Any] | None:
        cursor = await connection.execute(query, params)
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row is not None else None

    async def checkpoint_and_refine(
        self,
        project_id: str,
        completed_task_id: str,
    ) -> None:
        """
        After task completion, evaluate project state and potentially refine strategy.

        This is called after each task completion for auto-executing projects.
        It will:
        1. Check if the project needs strategy refinement
        2. Ask OpenClaw to analyze and suggest refinements
        3. Auto-apply refinements (based on design decision)
        4. Record decisions in journal
        """
        from cyborg.structured_logging import log_autonomy_decision

        project = await self.db.fetch_one(
            "SELECT id, state, auto_execute, metadata FROM projects WHERE id = ? AND deleted_at IS NULL",
            (project_id,),
        )
        if not project:
            return
        if project["state"] != ProjectState.ACTIVE.value or not bool(project.get("auto_execute", 0)):
            return

        # Check if project has auto-refinement enabled (default: yes)
        metadata = json_loads(project.get("metadata"), {})
        if metadata.get("auto_refine", True) is False:
            log_autonomy_decision(
                _get_structured_logger(),
                "refinement_skipped",
                project_id,
                reason="auto_refine disabled in metadata",
                trigger_task_id=completed_task_id,
            )
            return

        # Trigger strategy refinement
        try:
            log_autonomy_decision(
                _get_structured_logger(),
                "refinement_started",
                project_id,
                trigger_task_id=completed_task_id,
            )

            refinement = await self.reasoning_service.refine_project_strategy(
                project_id,
                completed_task_id,
            )

            # Record the analysis in journal
            await self._add_refinement_journal(
                project_id,
                completed_task_id,
                refinement,
            )

            # Log the refinement decision
            log_autonomy_decision(
                _get_structured_logger(),
                "refinement_completed",
                project_id,
                trigger_task_id=completed_task_id,
                should_refine=refinement.get("should_refine", False),
                suggested_changes_count=len(refinement.get("suggested_changes", [])),
                risks_identified_count=len(refinement.get("risks_identified", [])),
            )

            # Auto-apply refinements if suggested (design decision: auto-accept)
            if refinement.get("should_refine"):
                await self._apply_refinements(project_id, refinement, completed_task_id)

        except Exception as e:
            # Log but don't fail - refinement is optional
            logger.error(f"Strategy refinement failed for project {project_id}: {e}")

            # Log structured error
            log_autonomy_decision(
                _get_structured_logger(),
                "refinement_failed",
                project_id,
                trigger_task_id=completed_task_id,
                error_type=type(e).__name__,
                error_message=str(e),
            )

            # Record failure in journal
            await self._add_journal_entry(
                project_id,
                JournalEntryType.NOTE,
                f"Strategy refinement failed: {str(e)}",
            )

    async def _add_refinement_journal(
        self,
        project_id: str,
        trigger_task_id: str,
        refinement: dict[str, Any],
    ) -> None:
        """Record strategy refinement analysis in project journal."""
        from uuid import uuid4

        content_parts = [
            f"Strategy refinement triggered by task completion: {trigger_task_id}",
            "",
            f"Should refine: {refinement.get('should_refine', False)}",
            f"Reasoning: {refinement.get('reasoning', 'No reasoning provided')}",
        ]

        if refinement.get("suggested_changes"):
            content_parts.extend([
                "",
                "Suggested changes:",
            ])
            for i, change in enumerate(refinement["suggested_changes"], 1):
                content_parts.append(f"  {i}. {change.get('type', 'unknown')}: {change.get('description', '')}")

        if refinement.get("risks_identified"):
            content_parts.extend([
                "",
                "Risks identified:",
            ])
            for risk in refinement["risks_identified"]:
                content_parts.append(f"  - {risk}")

        await self._add_journal_entry(
            project_id,
            JournalEntryType.DECISION,
            "\n".join(content_parts),
            {
                "refinement": refinement,
                "trigger_task_id": trigger_task_id,
            },
        )

    async def _apply_refinements(
        self,
        project_id: str,
        refinement: dict[str, Any],
        trigger_task_id: str,
    ) -> None:
        """
        Auto-apply strategy refinements.

        Based on design decision: refinements are auto-accepted.
        This modifies the project plan, creates/removes tasks, or changes priorities.
        """
        from cyborg.structured_logging import log_autonomy_decision
        from uuid import uuid4
        from cyborg.services.task_service import TaskService
        from cyborg.models import TaskCreate, TaskPriority, TaskUpdate

        task_service = TaskService(self.db)
        changes_applied = []

        for change in refinement.get("suggested_changes", []):
            change_type = change.get("type")

            if change_type == "add_task":
                # Create new task
                task_payload = TaskCreate(
                    title=change.get("description", "Refinement task")[:200],
                    description=f"Auto-generated task from strategy refinement.\n\nReasoning: {refinement.get('reasoning', '')}",
                    plan=f"Address: {change.get('description', '')}",
                    priority=TaskPriority.HIGH,
                    project_ids=[project_id],
                    metadata={
                        "auto_created_by_project": True,
                        "autonomy_reason": "strategy_refinement",
                        "trigger_task_id": trigger_task_id,
                        "refinement_data": change,
                    },
                )

                task = await task_service.create_task(task_payload)
                await self._auto_start_refinement_task(task_service, str(task.id), project_id)
                changes_applied.append(f"Created task: {task.title}")

            elif change_type == "reprioritize":
                # Update task priority
                new_priorities = refinement.get("new_priorities", {})
                for task_id_str, priority in new_priorities.items():
                    await task_service.update_task(
                        task_id_str,
                        TaskUpdate(priority=priority),
                    )
                    changes_applied.append(f"Reprioritized task {task_id_str} to {priority}")

            elif change_type == "change_approach":
                # Record approach change - may need plan updates
                await self._add_journal_entry(
                    project_id,
                    JournalEntryType.DECISION,
                    f"Approach change recommended: {change.get('description', '')}",
                    {"change": change},
                )
                changes_applied.append(f"Approach change: {change.get('description', '')}")

        # Record what was applied
        if changes_applied:
            await self._add_journal_entry(
                project_id,
                JournalEntryType.MILESTONE,
                f"Applied {len(changes_applied)} refinement changes:\n" + "\n".join(f"  - {c}" for c in changes_applied),
                {"changes_applied": changes_applied, "refinement": refinement},
            )

            # Log the applied refinements
            log_autonomy_decision(
                _get_structured_logger(),
                "refinement_applied",
                project_id,
                trigger_task_id=trigger_task_id,
                changes_count=len(changes_applied),
                changes_applied=changes_applied[:5],  # Limit logged changes
            )

    async def _auto_start_refinement_task(
        self, task_service: Any, task_id: str, project_id: str
    ) -> None:
        """Start a refinement task if its project is auto-executing."""
        project = await self.db.fetch_one(
            "SELECT auto_execute FROM projects WHERE id = ? AND deleted_at IS NULL",
            (project_id,),
        )
        if project and bool(project.get("auto_execute", 0)):
            try:
                await task_service.start_task(task_id)
            except Exception:
                logger.warning(
                    "Auto-start failed for refinement task %s", task_id, exc_info=True
                )

    async def _add_journal_entry(
        self,
        project_id: str,
        entry_type: JournalEntryType,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Add a journal entry to a project."""
        from uuid import uuid4

        entry_id = str(uuid4())
        now = utcnow().isoformat()
        await self.db.execute(
            """
            INSERT INTO project_journal_entries (id, project_id, entry_type, content, created_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (entry_id, project_id, entry_type.value, content, now, json_dumps(metadata or {})),
        )
