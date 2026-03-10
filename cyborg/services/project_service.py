"""Business logic for project management."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from aiosqlite import Connection

from cyborg.database import Database
from cyborg.exceptions import NotFoundError
from cyborg.models import (
    ProjectCloseRequest,
    ProjectCreate,
    ProjectJournalEntryCreate,
    ProjectJournalEntryResponse,
    ProjectResponse,
    ProjectState,
    ProjectUpdate,
    TaskResponse,
)
from cyborg.services.base import BaseService, json_dumps, json_loads, utcnow


class ProjectService(BaseService):
    """CRUD and lifecycle operations for projects."""

    def __init__(self, db: Database) -> None:
        super().__init__(db)

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
            row["task_ids"] = await self._get_task_ids(row["id"])
            projects.append(ProjectResponse.model_validate(row))
        return projects

    async def get_project(self, project_id: str) -> ProjectResponse:
        row = await self._get_project_row(project_id)
        row["task_ids"] = await self._get_task_ids(project_id)
        return ProjectResponse.model_validate(row)

    async def create_project(self, payload: ProjectCreate) -> ProjectResponse:
        project_id = str(uuid4())
        now = utcnow().isoformat()
        async with self.db.connection(write=True) as connection:
            await self._validate_task_ids(connection, payload.task_ids)
            await connection.execute(
                """
                INSERT INTO projects (
                    id, title, description, aim, state, created_at, started_at, paused_at, closed_at, conclusion, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, NULL)
                """,
                (
                    project_id,
                    payload.title,
                    payload.description,
                    payload.aim,
                    payload.state.value,
                    now,
                    payload.conclusion,
                ),
            )
            await self._replace_task_links(connection, project_id, payload.task_ids)
        return await self.get_project(project_id)

    async def update_project(self, project_id: str, payload: ProjectUpdate) -> ProjectResponse:
        await self._get_project_row(project_id)
        values = payload.model_dump(exclude_unset=True, mode="json")
        task_ids = values.pop("task_ids", None)
        if not values and task_ids is None:
            return await self.get_project(project_id)

        if values.get("state") == ProjectState.ACTIVE.value and "started_at" not in values:
            values["started_at"] = utcnow().isoformat()
        if values.get("state") == ProjectState.PAUSED.value and "paused_at" not in values:
            values["paused_at"] = utcnow().isoformat()
        if values.get("state") == ProjectState.CLOSED.value and "closed_at" not in values:
            values["closed_at"] = utcnow().isoformat()
        assignments = ", ".join(f"{field} = ?" for field in values)
        params = tuple(values.values()) + (project_id,)

        async with self.db.connection(write=True) as connection:
            if assignments:
                await connection.execute(f"UPDATE projects SET {assignments} WHERE id = ? AND deleted_at IS NULL", params)
            if task_ids is not None:
                await self._validate_task_ids(connection, task_ids)
                await self._replace_task_links(connection, project_id, task_ids)
        return await self.get_project(project_id)

    async def delete_project(self, project_id: str) -> None:
        await self._get_project_row(project_id)
        now = utcnow().isoformat()
        await self.db.execute(
            "UPDATE projects SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
            (now, project_id),
        )

    async def start_project(self, project_id: str) -> ProjectResponse:
        return await self._transition_project(project_id, ProjectState.ACTIVE)

    async def pause_project(self, project_id: str) -> ProjectResponse:
        return await self._transition_project(project_id, ProjectState.PAUSED)

    async def close_project(self, project_id: str, payload: ProjectCloseRequest) -> ProjectResponse:
        await self._get_project_row(project_id)
        now = utcnow().isoformat()
        await self.db.execute(
            """
            UPDATE projects
            SET state = ?, closed_at = ?, conclusion = ?
            WHERE id = ? AND deleted_at IS NULL
            """,
            (ProjectState.CLOSED.value, now, payload.conclusion, project_id),
        )
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
        return ProjectJournalEntryResponse.model_validate(decoded)

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
            tasks.append(TaskResponse.model_validate(row))
        return tasks

    async def _transition_project(self, project_id: str, state: ProjectState) -> ProjectResponse:
        await self._get_project_row(project_id)
        now = utcnow().isoformat()
        updates: dict[str, Any] = {"state": state.value}
        if state == ProjectState.ACTIVE:
            updates["started_at"] = now
        if state == ProjectState.PAUSED:
            updates["paused_at"] = now
        assignments = ", ".join(f"{field} = ?" for field in updates)
        await self.db.execute(
            f"UPDATE projects SET {assignments} WHERE id = ? AND deleted_at IS NULL",
            tuple(updates.values()) + (project_id,),
        )
        return await self.get_project(project_id)

    async def _get_project_row(self, project_id: str) -> dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM projects WHERE id = ? AND deleted_at IS NULL", (project_id,))
        if row is None:
            raise NotFoundError(f"Project '{project_id}' was not found")
        return row

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
