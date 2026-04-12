"""Business logic for project management."""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiosqlite import Connection

from cyborg.config import Settings
from cyborg.database import Database
from cyborg.exceptions import ConflictError, NotFoundError
from cyborg.models import (
    NotificationEntityType,
    NotificationStatus,
    ProjectSpecSubmitRequest,
    ProjectCloseRequest,
    ProjectCreate,
    ProjectJournalEntryCreate,
    ProjectJournalEntryResponse,
    ProjectResponse,
    ProjectState,
    ProjectUpdate,
    TaskResponse,
)
from cyborg.services.notification_service import NotificationService
from cyborg.services.base import BaseService, json_dumps, json_loads, utcnow
from cyborg.services.project_spec_service import ProjectSpecService
from cyborg.services.session_route_service import (
    has_source_route_metadata,
    merge_source_route_metadata,
)

logger = logging.getLogger(__name__)


def _slugify(text: str) -> str:
    """Convert text to a URL-friendly slug."""
    text = text.lower()
    text = re.sub(r'[^a-z0-9]+', '-', text)
    # Remove leading/trailing hyphens
    text = text.strip('-')
    # Collapse multiple hyphens
    text = re.sub(r'-+', '-', text)
    return text


def short_task_id(task_id: str) -> str:
    """Return first 8 chars of a UUID with hyphens stripped."""
    return task_id.replace("-", "")[:8]


class ProjectService(BaseService):
    """CRUD and lifecycle operations for projects."""

    def __init__(self, db: Database) -> None:
        super().__init__(db)
        self._project_spec_service: ProjectSpecService | None = None

    @property
    def project_spec_service(self) -> ProjectSpecService:
        if self._project_spec_service is None:
            self._project_spec_service = ProjectSpecService(self.db)
        return self._project_spec_service

    async def _sync_notifications(self, project_id: str) -> None:
        await NotificationService(self.db).sync_project_state(project_id)

    async def list_projects(self, *, state: ProjectState | None = None) -> list[ProjectResponse]:
        query = "SELECT * FROM projects WHERE deleted_at IS NULL"
        params: list[Any] = []
        if state is not None:
            query += " AND state = ?"
            params.append(state.value)
        query += " ORDER BY created_at DESC"
        rows = await self.db.fetch_all(query, tuple(params))
        projects = []
        for row in rows:
            projects.append(ProjectResponse.model_validate(await self._decode_project_row(row)))
        return projects

    async def get_project(self, project_id: str) -> ProjectResponse:
        row = await self._get_project_row(project_id)
        return ProjectResponse.model_validate(await self._decode_project_row(row))

    async def create_project(self, payload: ProjectCreate | dict[str, Any], *, defer_effects: bool = False) -> ProjectResponse:
        payload = ProjectCreate.model_validate(payload)
        if payload.state == ProjectState.ACTIVE:
            raise ConflictError("Projects cannot be created directly in 'active' state. Create the project, submit a spec, approve it, then start or execute it.")

        project_id = str(uuid4())
        now = utcnow().isoformat()
        spec_payload = self._build_spec_payload(
            aim=payload.aim,
            method=payload.method,
            plan=payload.plan,
            success_criteria=payload.success_criteria,
        )
        async with self.db.connection(write=True) as connection:
            await self._validate_task_ids(connection, payload.task_ids)
            project_metadata = await self._inherit_task_source_route_metadata(
                connection,
                payload.metadata,
                payload.task_ids,
            )
            if self._require_source_route_metadata() and not has_source_route_metadata(project_metadata):
                raise ConflictError(
                    "Projects require source routing metadata. "
                    "Provide metadata.channel plus session_key/chat_id, or link an existing routed task."
                )
            await connection.execute(
                """
                INSERT INTO projects (
                    id, title, description, aim, method, state, plan, success_criteria,
                    created_at, updated_at, started_at, paused_at, closed_at, conclusion, deleted_at, metadata, current_spec_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, NULL, ?, NULL)
                """,
                (
                    project_id,
                    payload.title,
                    payload.description,
                    payload.aim,
                    payload.method,
                    payload.state.value,
                    json_dumps([step.model_dump(mode="json") for step in payload.plan]) if payload.plan else None,
                    json_dumps([c.model_dump(mode="json") for c in payload.success_criteria]) if payload.success_criteria else None,
                    now,
                    now,
                    payload.conclusion,
                    json_dumps(project_metadata),
                ),
            )
            await self._replace_task_links(connection, project_id, payload.task_ids)
        # Spec submission is fast (DB-only), keep synchronous so the response
        # includes the latest spec status.  Defer plan generation to the
        # background task so OpenClaw reasoning doesn't block the HTTP response.
        if spec_payload is not None:
            await self.project_spec_service.submit_spec(
                project_id, spec_payload, defer_plan_generation=defer_effects
            )
        if defer_effects:
            project = await self.get_project(project_id)
            return project
        await self._post_create_background_effects(
            project_id,
            has_spec=spec_payload is not None,
            initial_state=payload.state,
        )
        project = await self.get_project(project_id)
        return project

    async def _post_create_background_effects(self, project_id: str, *, has_spec: bool, initial_state: ProjectState) -> None:
        """Run notification sync, plan generation, and summary update in the background."""
        if has_spec:
            await self.project_spec_service.generate_plan_if_needed(project_id)
        else:
            await self._sync_notifications(project_id)
        project = await self.get_project(project_id)
        await self._update_summary_md(project)

    async def update_project(self, project_id: str, payload: ProjectUpdate, *, defer_plan_generation: bool = False) -> ProjectResponse:
        existing = await self._get_project_row(project_id)
        values = payload.model_dump(exclude_unset=True, mode="json")
        task_ids = values.pop("task_ids", None)
        raw_aim = values.pop("aim", None)
        raw_method = values.pop("method", None)
        raw_plan = values.pop("plan", None)
        raw_success_criteria = values.pop("success_criteria", None)
        spec_payload = self._build_spec_payload(
            aim=raw_aim,
            method=raw_method,
            plan=raw_plan,
            success_criteria=raw_success_criteria,
        )

        # Block spec-field updates on active projects — pause first
        has_spec_fields = any(v is not None for v in (raw_aim, raw_method, raw_plan, raw_success_criteria))
        if has_spec_fields and existing["state"] == ProjectState.ACTIVE.value:
            raise ConflictError(
                "Cannot update project spec fields (aim/method/plan/success_criteria) "
                "while the project is active. "
                "Pause the project first, then update the aim/method/plan."
            )

        if spec_payload is None:
            if raw_aim is not None:
                values["aim"] = raw_aim
            if raw_method is not None:
                values["method"] = raw_method
            if raw_plan is not None:
                values["plan"] = json_dumps(raw_plan)
            if raw_success_criteria is not None:
                values["success_criteria"] = json_dumps(raw_success_criteria)


        # Convert plan and success_criteria to JSON
        if "metadata" in values and values["metadata"] is not None:
            values["metadata"] = json_dumps(values["metadata"])
            
        if not values and task_ids is None and spec_payload is None:
            return await self.get_project(project_id)

        values["updated_at"] = utcnow().isoformat()
        assignments = ", ".join(f"{field} = ?" for field in values)
        params = tuple(values.values()) + (project_id,)

        async with self.db.connection(write=True) as connection:
            if assignments:
                await connection.execute(f"UPDATE projects SET {assignments} WHERE id = ? AND deleted_at IS NULL", params)
            if task_ids is not None:
                await self._validate_task_ids(connection, task_ids)
                await self._replace_task_links(connection, project_id, task_ids)
        if spec_payload is not None:
            await self.project_spec_service.submit_spec(
                project_id, spec_payload, defer_plan_generation=defer_plan_generation
            )
        else:
            await self._sync_notifications(project_id)
        project = await self.get_project(project_id)
        await self._update_summary_md(project)
        return project

    async def delete_project(self, project_id: str) -> None:
        """Hard-delete a project and cascade to all related data.

        Removes the project, orphan tasks, notifications, specs, journal entries,
        and the workspace directory. Tasks shared with other projects are unlinked
        but not deleted.
        """
        row = await self._get_project_row(project_id)
        slug = _slugify(row["title"])
        task_ids = await self._get_task_ids(project_id)

        notification_service = NotificationService(self.db)
        now = utcnow()

        async with self.db.connection(write=True) as conn:
            # 1. Resolve and delete all notifications for this project
            await conn.execute(
                "UPDATE notifications SET status = ?, resolved_at = ?, updated_at = ? "
                "WHERE entity_type = ? AND entity_id = ? AND status = ?",
                (NotificationStatus.RESOLVED.value, now.isoformat(), now.isoformat(),
                 NotificationEntityType.PROJECT.value, project_id, NotificationStatus.PENDING.value),
            )
            await conn.execute(
                "DELETE FROM notifications WHERE entity_type = ? AND entity_id = ?",
                (NotificationEntityType.PROJECT.value, project_id),
            )

            # 2. Find orphan tasks (only belong to this project) vs shared tasks
            orphan_task_ids: list[str] = []
            shared_task_ids: list[str] = []
            for tid in task_ids:
                cursor = await conn.execute(
                    "SELECT COUNT(DISTINCT project_id) AS cnt FROM project_tasks WHERE task_id = ?",
                    (tid,),
                )
                count_row = await cursor.fetchone()
                await cursor.close()
                if count_row and count_row["cnt"] <= 1:
                    orphan_task_ids.append(tid)
                else:
                    shared_task_ids.append(tid)

            # 3. Resolve and delete notifications for orphan tasks
            for tid in orphan_task_ids:
                await conn.execute(
                    "UPDATE notifications SET status = ?, resolved_at = ?, updated_at = ? "
                    "WHERE entity_type = ? AND entity_id = ? AND status = ?",
                    (NotificationStatus.RESOLVED.value, now.isoformat(), now.isoformat(),
                     NotificationEntityType.TASK.value, tid, NotificationStatus.PENDING.value),
                )
                await conn.execute(
                    "DELETE FROM notifications WHERE entity_type = ? AND entity_id = ?",
                    (NotificationEntityType.TASK.value, tid),
                )

            # 4. Delete orphan task data
            for tid in orphan_task_ids:
                await conn.execute("DELETE FROM task_steps WHERE task_id = ?", (tid,))
                await conn.execute("DELETE FROM task_history WHERE task_id = ?", (tid,))
                await conn.execute("DELETE FROM task_files WHERE task_id = ?", (tid,))
                await conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))

            # 5. Unlink all tasks from this project in the join table
            await conn.execute("DELETE FROM project_tasks WHERE project_id = ?", (project_id,))

            # 6. Delete specs and journal entries
            await conn.execute("DELETE FROM project_specs WHERE project_id = ?", (project_id,))
            await conn.execute("DELETE FROM project_journal_entries WHERE project_id = ?", (project_id,))

            # 7. Delete the project itself
            await conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))

        # 8. Delete workspace directory (outside transaction)
        workspace_path = Path(f"/home/mike/.openclaw/workspace/projects/{slug}")
        if workspace_path.exists():
            shutil.rmtree(workspace_path)
            logger.info("Deleted workspace directory: %s", workspace_path)

        logger.info(
            "Hard-deleted project %s: %d orphan tasks removed, %d shared tasks unlinked",
            project_id, len(orphan_task_ids), len(shared_task_ids),
        )

    async def pause_project(self, project_id: str) -> ProjectResponse:
        return await self._transition_project(project_id, ProjectState.PAUSED)

    async def close_project(self, project_id: str, payload: ProjectCloseRequest) -> ProjectResponse:
        await self._get_project_row(project_id)
        now = utcnow().isoformat()
        await self.db.execute(
            """
            UPDATE projects
            SET state = ?, closed_at = ?, conclusion = ?, updated_at = ?
            WHERE id = ? AND deleted_at IS NULL
            """,
            (ProjectState.CLOSED.value, now, payload.conclusion, now, project_id),
        )
        await self._sync_notifications(project_id)
        return await self.get_project(project_id)

    async def list_journal(self, project_id: str) -> list[ProjectJournalEntryResponse]:
        await self._get_project_row(project_id)
        rows = await self.db.fetch_all(
            "SELECT * FROM project_journal_entries WHERE project_id = ? ORDER BY created_at DESC",
            (project_id,),
        )
        decoded = [self.decode_json_fields(row, "metadata") for row in rows]
        return [ProjectJournalEntryResponse.model_validate(item) for item in decoded if item is not None]

    async def add_journal_entry(
        self,
        project_id: str,
        payload: ProjectJournalEntryCreate,
    ) -> ProjectJournalEntryResponse:
        await self._get_project_row(project_id)
        entry_id = str(uuid4())
        now = utcnow().isoformat()
        await self.db.execute(
            """
            INSERT INTO project_journal_entries (id, project_id, entry_type, content, created_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                entry_id,
                project_id,
                payload.entry_type.value,
                payload.content,
                now,
                json_dumps(payload.metadata),
            ),
        )
        row = await self.db.fetch_one("SELECT * FROM project_journal_entries WHERE id = ?", (entry_id,))
        decoded = self.decode_json_fields(row, "metadata")
        # Update summary after adding journal entry
        project = await self.get_project(project_id)
        await self._update_summary_md(project)
        return ProjectJournalEntryResponse.model_validate(decoded)

    async def _inherit_task_source_route_metadata(
        self,
        connection: Connection,
        metadata: dict[str, Any],
        task_ids: list[Any],
    ) -> dict[str, Any]:
        merged_metadata = dict(metadata or {})
        if has_source_route_metadata(merged_metadata):
            return merged_metadata

        unique_task_ids = list(dict.fromkeys(str(task_id) for task_id in task_ids))
        for task_id in unique_task_ids:
            cursor = await connection.execute(
                """
                SELECT t.metadata AS task_metadata, p.metadata AS project_metadata
                FROM tasks AS t
                LEFT JOIN project_tasks AS pt ON pt.task_id = t.id
                LEFT JOIN projects AS p ON p.id = pt.project_id AND p.deleted_at IS NULL
                WHERE t.id = ? AND t.deleted_at IS NULL
                ORDER BY p.created_at DESC
                LIMIT 1
                """,
                (task_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                continue
            row_dict = dict(row)

            merged_metadata = merge_source_route_metadata(
                merged_metadata,
                json_loads(row_dict.get("task_metadata"), {}),
            )
            merged_metadata = merge_source_route_metadata(
                merged_metadata,
                json_loads(row_dict.get("project_metadata"), {}),
            )
            if has_source_route_metadata(merged_metadata):
                return merged_metadata
        return merged_metadata

    def _require_source_route_metadata(self) -> bool:
        current = getattr(self.db, "settings", None)
        if isinstance(current, Settings):
            return current.openclaw.enabled
        return False

    async def list_project_tasks(self, project_id: str) -> list[TaskResponse]:
        await self._get_project_row(project_id)
        rows = await self.db.fetch_all(
            """
            SELECT t.*
            FROM tasks AS t
            INNER JOIN project_tasks AS pt ON pt.task_id = t.id
            WHERE pt.project_id = ? AND t.deleted_at IS NULL
            ORDER BY t.created_at DESC
            """,
            (project_id,),
        )
        tasks = []
        for row in rows:
            row["retry_config"] = json_loads(row["retry_config"], None)
            row["metadata"] = json_loads(row["metadata"], {})
            row["project_ids"] = await self._get_project_ids_for_task(row["id"])
            row.pop("current_plan_id", None)
            tasks.append(TaskResponse.model_validate(row))
        return tasks

    async def _transition_project(self, project_id: str, state: ProjectState) -> ProjectResponse:
        await self._get_project_row(project_id)
        if state == ProjectState.ACTIVE:
            await self.project_spec_service.ensure_project_ready_for_execution(project_id)
        now = utcnow().isoformat()
        updates: dict[str, Any] = {"state": state.value, "updated_at": now}
        if state == ProjectState.ACTIVE:
            updates["started_at"] = now
        if state == ProjectState.PAUSED:
            updates["paused_at"] = now
        assignments = ", ".join(f"{field} = ?" for field in updates)
        await self.db.execute(
            f"UPDATE projects SET {assignments} WHERE id = ? AND deleted_at IS NULL",
            tuple(updates.values()) + (project_id,),
        )
        await self._sync_notifications(project_id)
        return await self.get_project(project_id)

    async def _get_project_row(self, project_id: str) -> dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM projects WHERE id = ? AND deleted_at IS NULL", (project_id,))
        if row is None:
            raise NotFoundError(f"Project '{project_id}' was not found")
        return row

    async def _decode_project_row(self, row: dict[str, Any]) -> dict[str, Any]:
        decoded = dict(row)
        decoded.pop("auto_execute", None)
        decoded["task_ids"] = await self._get_task_ids(decoded["id"])
        decoded["plan"] = json_loads(decoded.get("plan"), [])
        decoded["success_criteria"] = json_loads(decoded.get("success_criteria"), [])
        decoded["metadata"] = json_loads(decoded.get("metadata"), {})
        decoded = await self.project_spec_service.populate_project_spec_fields(decoded)
        return decoded

    def _build_spec_payload(
        self,
        *,
        aim: str | None,
        method: str | None,
        plan: list[Any] | None,
        success_criteria: list[Any] | None,
    ) -> ProjectSpecSubmitRequest | None:
        if aim is None and method is None and plan is None and success_criteria is None:
            return None
        if not aim or not method or not success_criteria:
            return None
        return ProjectSpecSubmitRequest.model_validate(
            {
                "aim": aim,
                "method": method,
                "plan": plan or [],
                "success_criteria": success_criteria,
            }
        )

    async def _get_task_ids(self, project_id: str) -> list[str]:
        rows = await self.db.fetch_all(
            """
            SELECT pt.task_id
            FROM project_tasks AS pt
            INNER JOIN tasks AS t ON t.id = pt.task_id
            WHERE pt.project_id = ? AND t.deleted_at IS NULL
            ORDER BY pt.task_id
            """,
            (project_id,),
        )
        return [row["task_id"] for row in rows]

    async def _validate_task_ids(self, connection: Connection, task_ids: list[Any]) -> None:
        unique_task_ids = list(dict.fromkeys(str(task_id) for task_id in task_ids))
        if not unique_task_ids:
            return
        placeholders = ", ".join("?" for _ in unique_task_ids)
        cursor = await connection.execute(
            f"SELECT id FROM tasks WHERE id IN ({placeholders}) AND deleted_at IS NULL",
            tuple(unique_task_ids),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        if len(rows) != len(unique_task_ids):
            raise NotFoundError("One or more task_ids do not refer to active tasks")

    async def _replace_task_links(self, connection: Connection, project_id: str, task_ids: list[Any]) -> None:
        unique_task_ids = list(dict.fromkeys(str(task_id) for task_id in task_ids))
        await connection.execute("DELETE FROM project_tasks WHERE project_id = ?", (project_id,))
        if not unique_task_ids:
            return
        await connection.executemany(
            "INSERT INTO project_tasks (project_id, task_id) VALUES (?, ?)",
            [(project_id, task_id) for task_id in unique_task_ids],
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

    async def _get_task_files(self, task_id: str) -> list[dict[str, Any]]:
        """Fetch lightweight file records for a task."""
        rows = await self.db.fetch_all(
            "SELECT filename, purpose FROM task_files WHERE task_id = ? ORDER BY created_at ASC",
            (task_id,),
        )
        return [dict(r) for r in rows]

    async def get_project_path(self, project_id: str) -> Path:
        """Return the workspace directory path for a project.

        The path is derived from the project title slug:
        ``/home/mike/.openclaw/workspace/projects/<slug>/``

        The directory is created on disk if it does not already exist.
        """
        row = await self._get_project_row(project_id)
        slug = _slugify(row["title"])
        path = Path(f"/home/mike/.openclaw/workspace/projects/{slug}")
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def _update_summary_md(self, project: ProjectResponse) -> None:
        """Generate and write a SUMMARY.md file for the project.

        This creates a human-readable markdown report of the project including
        metadata, journal entries, and linked tasks.
        """
        # Build the summary content
        lines: list[str] = []

        # Header
        lines.append(f"# {project.title}")
        lines.append("")

        # Metadata section
        lines.append("## Project Metadata")
        lines.append("")
        lines.append(f"- **ID:** `{project.id}`")
        lines.append(f"- **State:** {project.state.value}")
        if project.aim:
            lines.append(f"- **Aim:** {project.aim}")
        if project.method:
            lines.append(f"- **Method:** {project.method}")
        if project.description:
            lines.append(f"- **Description:** {project.description}")
        lines.append(f"- **Created:** {project.created_at}")
        if project.started_at:
            lines.append(f"- **Started:** {project.started_at}")
        if project.paused_at:
            lines.append(f"- **Paused:** {project.paused_at}")
        if project.closed_at:
            lines.append(f"- **Closed:** {project.closed_at}")
        if project.conclusion:
            lines.append(f"- **Conclusion:** {project.conclusion}")
        lines.append("")

        # Linked tasks section (fetch early for plan progress calculation)
        lines.append("## Linked Tasks")
        lines.append("")

        tasks = await self.list_project_tasks(str(project.id))
        if tasks:
            for task in tasks:
                lines.append(f"### {task.title}")
                lines.append("")
                lines.append(f"- **ID:** `{task.id}`")
                lines.append(f"- **Status:** {task.status.value}")
                lines.append(f"- **Priority:** {task.priority.value}")
                if task.description:
                    lines.append(f"- **Description:** {task.description}")
                if task.requested_by:
                    lines.append(f"- **Requested By:** {task.requested_by}")
                lines.append(f"- **Created:** {task.created_at}")
                if task.completed_at:
                    lines.append(f"- **Completed:** {task.completed_at}")
                # Attach output files
                task_files = await self._get_task_files(str(task.id))
                if task_files:
                    lines.append("")
                    lines.append("**Output Files:**")
                    for f in task_files:
                        lines.append(f"  - `{f['filename']}` ({f['purpose']})")
                lines.append("")
        else:
            lines.append("*No linked tasks.*")
            lines.append("")

        # Plan progress section
        if project.plan:
            lines.append("## Plan Progress")
            lines.append("")
            
            # Count completed tasks for progress
            completed_count = 0
            for task in tasks:
                if task.status.value == "completed":
                    completed_count += 1
            
            for i, step in enumerate(project.plan):
                status = "✅" if i < completed_count else ("🔄" if i == completed_count else "⏳")
                lines.append(f"{status} **Step {i + 1}:** {step.title}")
                lines.append(f"   - {step.description}")
                lines.append(f"   - *Criteria:* {step.criteria}")
                lines.append("")
            
            lines.append(f"**Progress:** {completed_count}/{len(project.plan)} steps completed")
            lines.append("")

        # Success criteria section
        if project.success_criteria:
            lines.append("## Success Criteria")
            lines.append("")
            for criterion in project.success_criteria:
                lines.append(f"- **{criterion.description}**")
                lines.append(f"  - Check: `{criterion.check}`")
            lines.append("")

        # Journal entries section
        lines.append("## Journal Entries")
        lines.append("")

        journal_entries = await self.list_journal(str(project.id))
        if journal_entries:
            # Sort chronologically (oldest first)
            for entry in reversed(journal_entries):
                lines.append(f"### {entry.entry_type.value.title()} - {entry.created_at}")
                lines.append("")
                lines.append(entry.content)
                if entry.metadata:
                    lines.append("")
                    lines.append("**Metadata:**")
                    for key, value in entry.metadata.items():
                        lines.append(f"- `{key}`: {value}")
                lines.append("")
        else:
            lines.append("*No journal entries yet.*")
            lines.append("")

        # Write the file
        content = "\n".join(lines)
        slug = _slugify(project.title)
        summary_path = Path(f"/home/mike/.openclaw/workspace/projects/{slug}/SUMMARY.md")

        # Ensure directory exists
        summary_path.parent.mkdir(parents=True, exist_ok=True)

        # Write the file (complete replacement)
        summary_path.write_text(content, encoding="utf-8")
