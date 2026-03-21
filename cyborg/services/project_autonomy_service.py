"""Autonomous task/project progression after task completion."""

from __future__ import annotations

from typing import Any

from aiosqlite import Connection

from cyborg.database import Database
from cyborg.models import ProjectState, TaskStatus
from cyborg.services.base import BaseService, utcnow
from cyborg.services.notification_service import NotificationService


DEPENDENCY_BLOCKED_PREFIX = "Waiting for dependency task"


class ProjectAutonomyService(BaseService):
    """Release dependency-blocked tasks and checkpoint projects after task completion."""

    def __init__(self, db: Database, execution_service: Any | None = None) -> None:
        super().__init__(db)
        self._execution_service = execution_service

    @property
    def execution_service(self) -> Any:
        if self._execution_service is None:
            from cyborg.services.project_execution_service import ProjectExecutionService

            self._execution_service = ProjectExecutionService(self.db)
        return self._execution_service

    async def on_task_completed(self, task_id: str, task_title: str, result_summary: str | None = None) -> None:
        await self._release_unblocked_dependents(task_id)
        await self.execution_service.on_task_completed(task_id, task_title, result_summary)

        for project_id in await self._get_project_ids_for_task(task_id):
            await self._checkpoint_project(project_id)

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
                released_task_ids.append(row["id"])

        notification_service = NotificationService(self.db)
        for task_id in released_task_ids:
            await notification_service.sync_task_state(task_id, immediate=True)

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
              AND t.status IN (?, ?, ?, ?)
            LIMIT 1
            """,
            (
                project_id,
                TaskStatus.PLANNING.value,
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
        plan_row = await self._fetch_one_connection(
            connection,
            """
            SELECT 1 AS approved
            FROM plans AS p
            INNER JOIN tasks AS t ON t.current_plan_id = p.id
            WHERE t.id = ? AND p.status = 'approved'
            """,
            (task_id,),
        )
        if plan_row is not None:
            return TaskStatus.PENDING
        return TaskStatus.PLANNING

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
