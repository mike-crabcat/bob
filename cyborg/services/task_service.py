"""Business logic for task management."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from aiosqlite import Connection

from cyborg.database import Database
from cyborg.exceptions import ConflictError, NotFoundError
from cyborg.models import (
    RetryAction,
    RetryConfig,
    TaskCreate,
    TaskFailureRequest,
    TaskHistoryResponse,
    TaskResponse,
    TaskRetryRequest,
    TaskStatus,
    TaskStepCreate,
    TaskStepResponse,
    TaskStepStatus,
    TaskUpdate,
)
from cyborg.services.base import BaseService, json_dumps, json_loads, next_cron_occurrence, utcnow


class TaskService(BaseService):
    """CRUD and lifecycle operations for tasks."""

    def __init__(self, db: Database) -> None:
        super().__init__(db)

    async def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        parent_id: str | None = None,
    ) -> list[TaskResponse]:
        query = """
            SELECT *
            FROM tasks
            WHERE deleted_at IS NULL
        """
        params: list[Any] = []
        if status is not None:
            query += " AND status = ?"
            params.append(status.value)
        if parent_id is not None:
            query += " AND parent_id = ?"
            params.append(parent_id)
        query += " ORDER BY created_at DESC"
        rows = await self.db.fetch_all(query, tuple(params))
        tasks = []
        for row in rows:
            decoded = await self._decode_task_row(row)
            tasks.append(TaskResponse.model_validate(decoded))
        return tasks

    async def get_task(self, task_id: str) -> TaskResponse:
        row = await self._get_task_row(task_id)
        return TaskResponse.model_validate(await self._decode_task_row(row))

    async def create_task(self, payload: TaskCreate) -> TaskResponse:
        now = utcnow()
        task_id = str(uuid4())
        next_run_at = payload.next_run_at
        if payload.is_recurring and payload.recurrence_rule and next_run_at is None:
            next_run_at = next_cron_occurrence(payload.recurrence_rule, now)

        async with self.db.connection(write=True) as connection:
            await self._validate_project_ids(connection, payload.project_ids)
            await connection.execute(
                """
                INSERT INTO tasks (
                    id, title, description, requested_by, plan, status, priority,
                    parent_id, retry_config, is_recurring, recurrence_rule, next_run_at,
                    created_at, updated_at, started_at, completed_at, metadata, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    task_id,
                    payload.title,
                    payload.description,
                    payload.requested_by,
                    payload.plan,
                    payload.status.value,
                    payload.priority.value,
                    str(payload.parent_id) if payload.parent_id else None,
                    json_dumps(payload.retry_config.model_dump(mode="json")) if payload.retry_config else None,
                    int(payload.is_recurring),
                    payload.recurrence_rule,
                    next_run_at.isoformat() if next_run_at else None,
                    now.isoformat(),
                    now.isoformat(),
                    None,
                    None,
                    json_dumps(payload.metadata),
                ),
            )
            await self._replace_project_links(connection, task_id, payload.project_ids)
            await self._add_history(
                connection,
                task_id,
                "created",
                {"status": payload.status.value, "priority": payload.priority.value},
                now.isoformat(),
            )
        return await self.get_task(task_id)

    async def update_task(self, task_id: str, payload: TaskUpdate) -> TaskResponse:
        await self._get_task_row(task_id)
        values = payload.model_dump(exclude_unset=True, mode="json")
        project_ids = values.pop("project_ids", None)
        if not values and project_ids is None:
            return await self.get_task(task_id)

        now = utcnow()
        if values.get("is_recurring") and values.get("recurrence_rule") and values.get("next_run_at") is None:
            values["next_run_at"] = next_cron_occurrence(values["recurrence_rule"], now).isoformat()
        values["updated_at"] = now.isoformat()

        if "retry_config" in values and values["retry_config"] is not None:
            values["retry_config"] = json_dumps(values["retry_config"])
        if "metadata" in values and values["metadata"] is not None:
            values["metadata"] = json_dumps(values["metadata"])
        if "parent_id" in values and values["parent_id"] is not None:
            values["parent_id"] = str(values["parent_id"])
        if "status" in values and values["status"] == TaskStatus.ACTIVE.value and "started_at" not in values:
            values["started_at"] = now.isoformat()
        if "status" in values and values["status"] == TaskStatus.COMPLETED.value and "completed_at" not in values:
            values["completed_at"] = now.isoformat()

        assignments = ", ".join(f"{field} = ?" for field in values)
        params = tuple(values.values()) + (task_id,)

        async with self.db.connection(write=True) as connection:
            if assignments:
                await connection.execute(f"UPDATE tasks SET {assignments} WHERE id = ? AND deleted_at IS NULL", params)
            if project_ids is not None:
                await self._validate_project_ids(connection, project_ids)
                await self._replace_project_links(connection, task_id, project_ids)
            await self._add_history(connection, task_id, "updated", payload.model_dump(exclude_unset=True, mode="json"), now.isoformat())
        return await self.get_task(task_id)

    async def delete_task(self, task_id: str) -> None:
        await self._get_task_row(task_id)
        now = utcnow().isoformat()
        async with self.db.connection(write=True) as connection:
            await connection.execute(
                "UPDATE tasks SET deleted_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
                (now, now, task_id),
            )
            await self._add_history(connection, task_id, "deleted", {}, now)

    async def start_task(self, task_id: str) -> TaskResponse:
        return await self._transition_task(task_id, TaskStatus.ACTIVE, "started")

    async def complete_task(self, task_id: str) -> TaskResponse:
        row = await self._get_task_row(task_id)
        now = utcnow()
        next_run_at = row["next_run_at"]
        if row["is_recurring"] and row["recurrence_rule"]:
            next_run_at = next_cron_occurrence(row["recurrence_rule"], now).isoformat()

        async with self.db.connection(write=True) as connection:
            await connection.execute(
                """
                UPDATE tasks
                SET status = ?, completed_at = ?, updated_at = ?, next_run_at = ?
                WHERE id = ? AND deleted_at IS NULL
                """,
                (TaskStatus.COMPLETED.value, now.isoformat(), now.isoformat(), next_run_at, task_id),
            )
            await connection.execute(
                """
                UPDATE task_steps
                SET status = CASE WHEN status = ? THEN ? ELSE status END,
                    completed_at = CASE WHEN status = ? AND completed_at IS NULL THEN ? ELSE completed_at END
                WHERE task_id = ?
                """,
                (
                    TaskStepStatus.ACTIVE.value,
                    TaskStepStatus.COMPLETED.value,
                    TaskStepStatus.ACTIVE.value,
                    now.isoformat(),
                    task_id,
                ),
            )
            await self._add_history(connection, task_id, "completed", {"next_run_at": next_run_at}, now.isoformat())
        return await self.get_task(task_id)

    async def fail_task(self, task_id: str, payload: TaskFailureRequest) -> TaskResponse:
        task = await self.get_task(task_id)
        now = utcnow()
        retry_config = task.retry_config
        should_reload = False

        async with self.db.connection(write=True) as connection:
            await self._add_history(
                connection,
                task_id,
                "failed",
                {"details": payload.details, "result": payload.result},
                now.isoformat(),
            )

            if retry_config and retry_config.on_failure == RetryAction.RETRY_FROM:
                updated_config = retry_config.model_copy(update={"current_attempt": retry_config.current_attempt + 1})
                if updated_config.current_attempt <= updated_config.max_attempts:
                    await self._reset_steps_from_retry_point(connection, task_id, updated_config.retry_from_step, now.isoformat())
                    await connection.execute(
                        """
                        UPDATE tasks
                        SET status = ?, retry_config = ?, updated_at = ?, started_at = COALESCE(started_at, ?)
                        WHERE id = ? AND deleted_at IS NULL
                        """,
                        (
                            TaskStatus.ACTIVE.value,
                            json_dumps(updated_config.model_dump(mode="json")),
                            now.isoformat(),
                            now.isoformat(),
                            task_id,
                        ),
                    )
                    await self._add_history(
                        connection,
                        task_id,
                        "retry_from_step",
                        {"retry_from_step": updated_config.retry_from_step, "attempt": updated_config.current_attempt},
                        now.isoformat(),
                    )
                    should_reload = True

            if not should_reload:
                await connection.execute(
                    """
                    UPDATE tasks
                    SET status = ?, updated_at = ?, completed_at = ?
                    WHERE id = ? AND deleted_at IS NULL
                    """,
                    (TaskStatus.FAILED.value, now.isoformat(), now.isoformat(), task_id),
                )
                await connection.execute(
                    """
                    UPDATE task_steps
                    SET status = ?, result = COALESCE(?, result), completed_at = COALESCE(completed_at, ?)
                    WHERE task_id = ? AND status = ?
                    """,
                    (
                        TaskStepStatus.FAILED.value,
                        payload.result,
                        now.isoformat(),
                        task_id,
                        TaskStepStatus.ACTIVE.value,
                    ),
                )
        return await self.get_task(task_id)

    async def retry_task(self, task_id: str, payload: TaskRetryRequest) -> TaskResponse:
        task = await self.get_task(task_id)
        now = utcnow()
        retry_config = task.retry_config or RetryConfig()
        updated_config = retry_config.model_copy(update={"current_attempt": retry_config.current_attempt + 1})
        if updated_config.current_attempt > updated_config.max_attempts:
            raise ConflictError(f"Task '{task_id}' has exhausted its retry budget")

        async with self.db.connection(write=True) as connection:
            if updated_config.on_failure == RetryAction.RETRY_FROM and updated_config.retry_from_step is not None:
                await self._reset_steps_from_retry_point(connection, task_id, updated_config.retry_from_step, now.isoformat())
            else:
                await connection.execute(
                    """
                    UPDATE task_steps
                    SET status = ?, result = NULL, started_at = NULL, completed_at = NULL
                    WHERE task_id = ? AND status = ?
                    """,
                    (TaskStepStatus.PENDING.value, task_id, TaskStepStatus.FAILED.value),
                )

            await connection.execute(
                """
                UPDATE tasks
                SET status = ?, retry_config = ?, updated_at = ?, completed_at = NULL, started_at = COALESCE(started_at, ?)
                WHERE id = ? AND deleted_at IS NULL
                """,
                (
                    TaskStatus.ACTIVE.value,
                    json_dumps(updated_config.model_dump(mode="json")),
                    now.isoformat(),
                    now.isoformat(),
                    task_id,
                ),
            )
            await self._add_history(connection, task_id, "retried", payload.model_dump(mode="json"), now.isoformat())
        return await self.get_task(task_id)

    async def list_steps(self, task_id: str) -> list[TaskStepResponse]:
        await self._get_task_row(task_id)
        rows = await self.db.fetch_all(
            "SELECT * FROM task_steps WHERE task_id = ? ORDER BY step_number ASC",
            (task_id,),
        )
        return [TaskStepResponse.model_validate(row) for row in rows]

    async def upsert_step(self, task_id: str, payload: TaskStepCreate) -> TaskStepResponse:
        await self._get_task_row(task_id)
        now = utcnow().isoformat()
        started_at = payload.started_at.isoformat() if payload.started_at else (now if payload.status == TaskStepStatus.ACTIVE else None)
        completed_at = payload.completed_at.isoformat() if payload.completed_at else (
            now if payload.status in {TaskStepStatus.COMPLETED, TaskStepStatus.FAILED} else None
        )

        async with self.db.connection(write=True) as connection:
            existing = await self._fetch_one_connection(
                connection,
                "SELECT * FROM task_steps WHERE task_id = ? AND step_number = ?",
                (task_id, payload.step_number),
            )
            if existing:
                await connection.execute(
                    """
                    UPDATE task_steps
                    SET description = ?, status = ?, result = ?, started_at = ?, completed_at = ?
                    WHERE task_id = ? AND step_number = ?
                    """,
                    (
                        payload.description,
                        payload.status.value,
                        payload.result,
                        started_at,
                        completed_at,
                        task_id,
                        payload.step_number,
                    ),
                )
                step_id = existing["id"]
            else:
                step_id = str(uuid4())
                await connection.execute(
                    """
                    INSERT INTO task_steps (id, task_id, step_number, description, status, result, started_at, completed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        step_id,
                        task_id,
                        payload.step_number,
                        payload.description,
                        payload.status.value,
                        payload.result,
                        started_at,
                        completed_at,
                    ),
                )
            await connection.execute(
                "UPDATE tasks SET updated_at = ? WHERE id = ? AND deleted_at IS NULL",
                (now, task_id),
            )
            await self._add_history(
                connection,
                task_id,
                "step_upserted",
                {"step_number": payload.step_number, "status": payload.status.value},
                now,
            )
        rows = await self.db.fetch_all("SELECT * FROM task_steps WHERE id = ?", (step_id,))
        return TaskStepResponse.model_validate(rows[0])

    async def create_subtask(self, task_id: str, payload: TaskCreate) -> TaskResponse:
        parent = await self.get_task(task_id)
        data = payload.model_copy(update={"parent_id": parent.id})
        return await self.create_task(data)

    async def list_history(self, task_id: str) -> list[TaskHistoryResponse]:
        await self._get_task_row(task_id)
        rows = await self.db.fetch_all(
            "SELECT * FROM task_history WHERE task_id = ? ORDER BY timestamp ASC",
            (task_id,),
        )
        history = [self.decode_json_fields(row, "details") for row in rows]
        return [TaskHistoryResponse.model_validate(item) for item in history if item is not None]

    async def _transition_task(self, task_id: str, status: TaskStatus, action: str) -> TaskResponse:
        await self._get_task_row(task_id)
        now = utcnow().isoformat()
        started_at = now if status == TaskStatus.ACTIVE else None

        async with self.db.connection(write=True) as connection:
            await connection.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?, started_at = COALESCE(started_at, ?)
                WHERE id = ? AND deleted_at IS NULL
                """,
                (status.value, now, started_at, task_id),
            )
            if status == TaskStatus.ACTIVE:
                await connection.execute(
                    """
                    UPDATE task_steps
                    SET status = ?, started_at = COALESCE(started_at, ?)
                    WHERE id = (
                        SELECT id FROM task_steps
                        WHERE task_id = ? AND status = ?
                        ORDER BY step_number ASC
                        LIMIT 1
                    )
                    """,
                    (TaskStepStatus.ACTIVE.value, now, task_id, TaskStepStatus.PENDING.value),
                )
            await self._add_history(connection, task_id, action, {"status": status.value}, now)
        return await self.get_task(task_id)

    async def _get_task_row(self, task_id: str) -> dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM tasks WHERE id = ? AND deleted_at IS NULL", (task_id,))
        if row is None:
            raise NotFoundError(f"Task '{task_id}' was not found")
        return row

    async def _decode_task_row(self, row: dict[str, Any]) -> dict[str, Any]:
        row["retry_config"] = json_loads(row.get("retry_config"), None)
        row["metadata"] = json_loads(row.get("metadata"), {})
        row["project_ids"] = await self._get_project_ids(row["id"])
        return row

    async def _get_project_ids(self, task_id: str) -> list[str]:
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

    async def _validate_project_ids(self, connection: Connection, project_ids: list[Any]) -> None:
        unique_project_ids = list(dict.fromkeys(str(project_id) for project_id in project_ids))
        if not unique_project_ids:
            return
        placeholders = ", ".join("?" for _ in unique_project_ids)
        cursor = await connection.execute(
            f"SELECT id FROM projects WHERE id IN ({placeholders}) AND deleted_at IS NULL",
            tuple(unique_project_ids),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        if len(rows) != len(unique_project_ids):
            raise NotFoundError("One or more project_ids do not refer to active projects")

    async def _replace_project_links(self, connection: Connection, task_id: str, project_ids: list[Any]) -> None:
        unique_project_ids = list(dict.fromkeys(str(project_id) for project_id in project_ids))
        await connection.execute("DELETE FROM project_tasks WHERE task_id = ?", (task_id,))
        if not unique_project_ids:
            return
        await connection.executemany(
            "INSERT INTO project_tasks (project_id, task_id) VALUES (?, ?)",
            [(project_id, task_id) for project_id in unique_project_ids],
        )

    async def _add_history(
        self,
        connection: Connection,
        task_id: str,
        action: str,
        details: dict[str, Any],
        timestamp: str,
    ) -> None:
        await connection.execute(
            "INSERT INTO task_history (id, task_id, action, details, timestamp) VALUES (?, ?, ?, ?, ?)",
            (str(uuid4()), task_id, action, json_dumps(details), timestamp),
        )

    async def _reset_steps_from_retry_point(
        self,
        connection: Connection,
        task_id: str,
        retry_from_step: int | None,
        timestamp: str,
    ) -> None:
        if retry_from_step is None:
            return
        retry_step = await self._fetch_one_connection(
            connection,
            "SELECT id FROM task_steps WHERE task_id = ? AND step_number = ?",
            (task_id, retry_from_step),
        )
        if retry_step is None:
            raise ConflictError(f"Task '{task_id}' does not have step {retry_from_step} for retry_from")
        await connection.execute(
            """
            UPDATE task_steps
            SET status = ?, result = NULL, started_at = NULL, completed_at = NULL
            WHERE task_id = ? AND step_number >= ?
            """,
            (TaskStepStatus.PENDING.value, task_id, retry_from_step),
        )
        await connection.execute(
            """
            UPDATE task_steps
            SET status = ?, started_at = ?
            WHERE task_id = ? AND step_number = ?
            """,
            (TaskStepStatus.ACTIVE.value, timestamp, task_id, retry_from_step),
        )

    async def _fetch_one_connection(
        self,
        connection: Connection,
        query: str,
        params: tuple[Any, ...] = (),
    ) -> dict[str, Any] | None:
        cursor = await connection.execute(query, params)
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row else None
