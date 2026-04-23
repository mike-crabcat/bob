"""Autonomous task/project progression after task completion."""

from __future__ import annotations

import logging
from typing import Any

from aiosqlite import Connection

from cyborg_server.database import Database
from cyborg_server.models import JournalEntryType, ProjectState, TaskStatus
from cyborg_server.services.base import BaseService, utcnow, json_dumps, json_loads


logger = logging.getLogger(__name__)

# Lazy import structured logging helpers
_structured_logger = None


def _get_structured_logger():
    """Lazy import structured logging helpers."""
    global _structured_logger
    if _structured_logger is None:
        from cyborg_server.structured_logging import get_logger as _get_logger
        _structured_logger = _get_logger(__name__)
    return _structured_logger


DEPENDENCY_BLOCKED_PREFIX = "Waiting for dependency task"


class ProjectAutonomyService(BaseService):
    """Release dependency-blocked tasks and checkpoint projects after task completion."""

    def __init__(self, db: Database, execution_service: Any | None = None) -> None:
        super().__init__(db)
        self._execution_service = execution_service

    @property
    def execution_service(self) -> Any:
        if self._execution_service is None:
            from cyborg_server.services.project_execution_service import ProjectExecutionService

            self._execution_service = ProjectExecutionService(self.db)
        return self._execution_service

    async def on_task_completed(
        self,
        task_id: str,
        task_title: str,
        result_summary: str | None = None,
    ) -> None:
        """Handle task completion: release dependents and trigger project progression."""
        await self._release_unblocked_dependents(task_id)
        await self.execution_service.on_task_completed(task_id, task_title, result_summary)

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

        Returns ACTIVE if the task belongs to a project, PENDING otherwise.
        """
        project_row = await self._fetch_one_connection(
            connection,
            """
            SELECT p.id
            FROM projects AS p
            INNER JOIN project_tasks AS pt ON pt.project_id = p.id
            WHERE pt.task_id = ? AND p.deleted_at IS NULL
            LIMIT 1
            """,
            (task_id,),
        )
        if project_row:
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
        from cyborg_server.services.base import json_dumps

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
