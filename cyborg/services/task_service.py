"""Business logic for task management."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from aiosqlite import Connection

from cyborg.database import Database
from cyborg.exceptions import ConflictError, NotFoundError
from cyborg.models import (
    PlanStatus,
    RetryAction,
    RetryConfig,
    TaskBlockRequest,
    TaskCreate,
    TaskFailureRequest,
    TaskHistoryResponse,
    TaskResponse,
    TaskRetryRequest,
    TaskStatus,
    TaskStepCreate,
    TaskStepResponse,
    TaskStepStatus,
    TaskUnblockRequest,
    TaskUpdate,
)
from cyborg.services.base import BaseService, json_dumps, json_loads, next_cron_occurrence, utcnow
from cyborg.services.notification_service import NotificationService
from cyborg.services.project_autonomy_service import DEPENDENCY_BLOCKED_PREFIX
from cyborg.services.session_route_service import SessionRouteService
from cyborg.services.webhook_service import WebhookEvent, WebhookService


class TaskService(BaseService):
    """CRUD and lifecycle operations for tasks."""

    def __init__(self, db: Database, webhook_service: WebhookService | None = None) -> None:
        super().__init__(db)
        self._webhook_service = webhook_service

    def _get_webhook_service(self) -> WebhookService | None:
        """Lazy-load webhook service."""
        if self._webhook_service is None:
            self._webhook_service = WebhookService(self.db)
        return self._webhook_service

    async def _sync_notifications(self, task_id: str, *, immediate: bool = False) -> None:
        await NotificationService(self.db).sync_task_state(task_id, immediate=immediate)

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

    async def resolve_target_session(self, task_id: str) -> dict[str, Any] | None:
        task = await self.get_task(task_id)
        return await self._resolve_target_session_from_metadata(task.metadata)

    async def create_task(self, payload: TaskCreate) -> TaskResponse:
        if payload.status != TaskStatus.PLANNING:
            raise ConflictError("New tasks must start in 'planning' status")

        now = utcnow()
        task_id = str(uuid4())
        plan_id = str(uuid4())
        next_run_at = payload.next_run_at
        if payload.is_recurring and payload.recurrence_rule and next_run_at is None:
            next_run_at = next_cron_occurrence(payload.recurrence_rule, now)

        async with self.db.connection(write=True) as connection:
            await self._validate_project_ids(connection, payload.project_ids)
            await self._validate_target_session_metadata(connection, payload.metadata)
            await connection.execute(
                """
                INSERT INTO tasks (
                    id, title, description, requested_by, plan, status, priority,
                    parent_id, retry_config, is_recurring, recurrence_rule, next_run_at,
                    created_at, updated_at, started_at, completed_at, metadata, deleted_at,
                    blocked_reason, blocked_resume_instructions
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
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
                    payload.blocked_reason,
                    payload.blocked_resume_instructions,
                ),
            )
            await self._replace_project_links(connection, task_id, payload.project_ids)
            await connection.execute(
                """
                INSERT INTO plans (
                    id, task_id, version_number, content, status,
                    feedback, created_at, approved_at, approved_by, is_current
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    task_id,
                    1,
                    payload.plan,
                    PlanStatus.PENDING_APPROVAL.value,
                    None,
                    now.isoformat(),
                    None,
                    None,
                    0,
                ),
            )
            await self._add_history(
                connection,
                task_id,
                "created",
                {"status": payload.status.value, "priority": payload.priority.value},
                now.isoformat(),
            )
        await self._sync_notifications(task_id, immediate=True)
        return await self.get_task(task_id)

    async def update_task(self, task_id: str, payload: TaskUpdate) -> TaskResponse:
        row = await self._get_task_row(task_id)
        values = payload.model_dump(exclude_unset=True, mode="json")
        project_ids = values.pop("project_ids", None)
        if not values and project_ids is None:
            return await self.get_task(task_id)

        now = utcnow()
        immediate_notification = False
        raw_metadata = values.get("metadata")
        if values.get("is_recurring") and values.get("recurrence_rule") and values.get("next_run_at") is None:
            values["next_run_at"] = next_cron_occurrence(values["recurrence_rule"], now).isoformat()
        values["updated_at"] = now.isoformat()

        if "retry_config" in values and values["retry_config"] is not None:
            values["retry_config"] = json_dumps(values["retry_config"])
        if "metadata" in values and values["metadata"] is not None:
            values["metadata"] = json_dumps(values["metadata"])
        if "parent_id" in values and values["parent_id"] is not None:
            values["parent_id"] = str(values["parent_id"])
        if "status" in values:
            next_status = TaskStatus(values["status"])
            await self._validate_status_transition(task_id, row["status"], next_status)
            immediate_notification = (
                next_status.value != row["status"] and next_status in {TaskStatus.PLANNING, TaskStatus.BLOCKED}
            )
            if next_status == TaskStatus.PLANNING:
                values["current_plan_id"] = None
            if next_status == TaskStatus.ACTIVE and "started_at" not in values:
                values["started_at"] = now.isoformat()
            if next_status == TaskStatus.COMPLETED and "completed_at" not in values:
                values["completed_at"] = now.isoformat()
        elif row["status"] == TaskStatus.BLOCKED.value and (
            "blocked_reason" in values or "blocked_resume_instructions" in values
        ):
            immediate_notification = True

        assignments = ", ".join(f"{field} = ?" for field in values)
        params = tuple(values.values()) + (task_id,)

        async with self.db.connection(write=True) as connection:
            if raw_metadata is not None:
                await self._validate_target_session_metadata(connection, raw_metadata)
            if assignments:
                await connection.execute(f"UPDATE tasks SET {assignments} WHERE id = ? AND deleted_at IS NULL", params)
            if project_ids is not None:
                await self._validate_project_ids(connection, project_ids)
                await self._replace_project_links(connection, task_id, project_ids)
            await self._add_history(connection, task_id, "updated", payload.model_dump(exclude_unset=True, mode="json"), now.isoformat())
        await self._sync_notifications(task_id, immediate=immediate_notification)
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
        await self._sync_notifications(task_id, immediate=False)

    async def start_task(self, task_id: str) -> TaskResponse:
        return await self._transition_task(task_id, TaskStatus.ACTIVE, "started")

    async def complete_task(self, task_id: str, result_summary: str | None = None) -> TaskResponse:
        row = await self._get_task_row(task_id)
        now = utcnow()
        next_run_at = row["next_run_at"]
        if row["is_recurring"] and row["recurrence_rule"]:
            next_run_at = next_cron_occurrence(row["recurrence_rule"], now).isoformat()

        async with self.db.connection(write=True) as connection:
            await connection.execute(
                """
                UPDATE tasks
                SET status = ?, result = COALESCE(?, result), completed_at = ?, updated_at = ?, next_run_at = ?
                WHERE id = ? AND deleted_at IS NULL
                """,
                (
                    TaskStatus.COMPLETED.value,
                    result_summary,
                    now.isoformat(),
                    now.isoformat(),
                    next_run_at,
                    task_id,
                ),
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
            await self._add_history(
                connection,
                task_id,
                "completed",
                {"next_run_at": next_run_at, "result": result_summary},
                now.isoformat(),
            )

            # Add journal entry to parent projects
            await self._add_completion_journal_entry(connection, task_id, row["title"], result_summary, now.isoformat())

        await self._sync_notifications(task_id, immediate=False)
        await NotificationService(self.db).create_task_result_notification(
            task_id,
            failed=False,
            result_summary=result_summary,
        )
        task_response = await self.get_task(task_id)

        # Trigger post-completion autonomy flow for linked tasks and projects.
        await self._trigger_project_execution(task_id, row["title"], result_summary)

        # Trigger webhook notification
        await self._trigger_webhook(
            event=WebhookEvent.TASK_COMPLETED,
            task_id=task_id,
            task_title=row["title"],
            result_summary=result_summary,
            metadata=json_loads(row.get("metadata"), {}),
        )

        return task_response

    async def _trigger_project_execution(self, task_id: str, task_title: str, result_summary: str | None = None) -> None:
        """Trigger project execution flow when a task completes.
        
        This notifies the project execution service to check if any linked
        projects should progress to the next step.
        """
        try:
            from cyborg.services.project_autonomy_service import ProjectAutonomyService

            autonomy_service = ProjectAutonomyService(self.db)
            await autonomy_service.on_task_completed(task_id, task_title, result_summary)
        except Exception:
            # Don't let project execution failures affect task completion
            pass

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
                    await self._verify_plan_approved(task_id, target_status=TaskStatus.ACTIVE)
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
                    SET status = ?, result = COALESCE(?, result), updated_at = ?, completed_at = ?
                    WHERE id = ? AND deleted_at IS NULL
                    """,
                    (
                        TaskStatus.FAILED.value,
                        payload.result,
                        now.isoformat(),
                        now.isoformat(),
                        task_id,
                    ),
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
        
        await self._sync_notifications(task_id, immediate=False)
        if not should_reload:
            await NotificationService(self.db).create_task_result_notification(
                task_id,
                failed=True,
                result_summary=payload.result,
            )
        task_response = await self.get_task(task_id)
        
        # Trigger webhook notification (only if actually failed, not retrying)
        if not should_reload:
            await self._trigger_webhook(
                event=WebhookEvent.TASK_FAILED,
                task_id=task_id,
                task_title=task.title,
                result_summary=payload.result,
                metadata=task.metadata,
            )
        
        return task_response

    async def retry_task(self, task_id: str, payload: TaskRetryRequest) -> TaskResponse:
        task = await self.get_task(task_id)
        now = utcnow()
        retry_config = task.retry_config or RetryConfig()
        updated_config = retry_config.model_copy(update={"current_attempt": retry_config.current_attempt + 1})
        if updated_config.current_attempt > updated_config.max_attempts:
            raise ConflictError(f"Task '{task_id}' has exhausted its retry budget")
        await self._verify_plan_approved(task_id, target_status=TaskStatus.ACTIVE)

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
        await self._sync_notifications(task_id, immediate=False)
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

    async def block_task(self, task_id: str, payload: TaskBlockRequest) -> TaskResponse:
        """Block a task waiting for user input.

        Records the reason and full resume instructions so the task can be
        resumed without relying on conversational context.
        """
        await self._get_task_row(task_id)
        now = utcnow()

        async with self.db.connection(write=True) as connection:
            await connection.execute(
                """
                UPDATE tasks
                SET status = ?, blocked_reason = ?, blocked_resume_instructions = ?, updated_at = ?
                WHERE id = ? AND deleted_at IS NULL
                """,
                (
                    TaskStatus.BLOCKED.value,
                    payload.reason,
                    payload.resume_instructions,
                    now.isoformat(),
                    task_id,
                ),
            )
            await self._add_history(
                connection,
                task_id,
                "blocked",
                {"reason": payload.reason},
                now.isoformat(),
            )
        await self._sync_notifications(task_id, immediate=True)
        return await self.get_task(task_id)

    async def unblock_task(self, task_id: str, payload: TaskUnblockRequest) -> TaskResponse:
        """Unblock a task and resume work."""
        row = await self._get_task_row(task_id)
        if row["status"] != TaskStatus.BLOCKED.value:
            raise ConflictError(f"Task '{task_id}' is not blocked (status: {row['status']})")

        now = utcnow()
        resumed_status = await self._determine_unblocked_status(task_id, row)

        async with self.db.connection(write=True) as connection:
            if resumed_status == TaskStatus.ACTIVE:
                await connection.execute(
                    """
                    UPDATE tasks
                    SET status = ?, updated_at = ?, started_at = COALESCE(started_at, ?)
                    WHERE id = ? AND deleted_at IS NULL
                    """,
                    (
                        resumed_status.value,
                        now.isoformat(),
                        now.isoformat(),
                        task_id,
                    ),
                )
            else:
                await connection.execute(
                    """
                    UPDATE tasks
                    SET status = ?, updated_at = ?
                    WHERE id = ? AND deleted_at IS NULL
                    """,
                    (
                        resumed_status.value,
                        now.isoformat(),
                        task_id,
                    ),
                )
            await self._add_history(
                connection,
                task_id,
                "unblocked",
                {"status": resumed_status.value, "notes": payload.notes} if payload.notes else {"status": resumed_status.value},
                now.isoformat(),
            )
        await self._sync_notifications(task_id, immediate=False)
        return await self.get_task(task_id)

    async def list_history(self, task_id: str) -> list[TaskHistoryResponse]:
        await self._get_task_row(task_id)
        rows = await self.db.fetch_all(
            "SELECT * FROM task_history WHERE task_id = ? ORDER BY timestamp ASC",
            (task_id,),
        )
        history = [self.decode_json_fields(row, "details") for row in rows]
        return [TaskHistoryResponse.model_validate(item) for item in history if item is not None]

    async def _transition_task(self, task_id: str, status: TaskStatus, action: str) -> TaskResponse:
        row = await self._get_task_row(task_id)
        now = utcnow().isoformat()
        started_at = now if status == TaskStatus.ACTIVE else None

        # Check plan approval if transitioning to active
        if status == TaskStatus.ACTIVE:
            if row["status"] != TaskStatus.PENDING.value:
                raise ConflictError(
                    f"Cannot transition task '{task_id}' from '{row['status']}' to 'active'. "
                    "Task must be in 'pending' status."
                )
            await self._verify_plan_approved(task_id, target_status=status)

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
        await self._sync_notifications(task_id, immediate=False)
        return await self.get_task(task_id)

    async def _verify_plan_approved(self, task_id: str, *, target_status: TaskStatus) -> None:
        """Verify that a task has an approved current plan before transition.
        
        Raises ConflictError if no approved plan exists.
        """
        # Check if there's an approved plan for this task
        plan_row = await self.db.fetch_one(
            """
            SELECT p.status 
            FROM plans p
            INNER JOIN tasks t ON t.current_plan_id = p.id
            WHERE t.id = ? AND p.status = ?
            """,
            (task_id, PlanStatus.APPROVED.value),
        )
        
        if plan_row is None:
            # Check if there's any plan at all
            any_plan = await self.db.fetch_one(
                "SELECT status FROM plans WHERE task_id = ? ORDER BY version_number DESC LIMIT 1",
                (task_id,),
            )
            
            if any_plan is None:
                raise ConflictError(
                    f"Cannot transition task '{task_id}' to '{target_status.value}': "
                    "no approved plan is available. Submit a plan and have it approved first."
                )
            else:
                raise ConflictError(
                    f"Cannot transition task '{task_id}' to '{target_status.value}': "
                    f"current plan is not approved (latest plan status: {any_plan['status']}). "
                    "Wait for approval or submit a new plan."
                )

    async def _validate_status_transition(self, task_id: str, current_status: str, next_status: TaskStatus) -> None:
        if current_status == next_status.value:
            return
        if next_status == TaskStatus.PENDING:
            await self._verify_plan_approved(task_id, target_status=next_status)
            row = await self._get_task_row(task_id)
            if not await self._dependency_is_satisfied(row):
                raise ConflictError(
                    f"Cannot transition task '{task_id}' to 'pending' while its dependency is incomplete."
                )
            return
        if next_status == TaskStatus.ACTIVE:
            if current_status != TaskStatus.PENDING.value:
                raise ConflictError(
                    f"Cannot transition task '{task_id}' from '{current_status}' to 'active'. "
                    "Task must be in 'pending' status."
                )
            await self._verify_plan_approved(task_id, target_status=next_status)
            row = await self._get_task_row(task_id)
            if not await self._dependency_is_satisfied(row):
                raise ConflictError(
                    f"Cannot transition task '{task_id}' to 'active' while its dependency is incomplete."
                )

    async def _determine_unblocked_status(self, task_id: str, row: dict[str, Any]) -> TaskStatus:
        if not await self._dependency_is_satisfied(row):
            return TaskStatus.BLOCKED
        if await self._has_approved_current_plan(task_id):
            if row.get("started_at"):
                return TaskStatus.ACTIVE
            return TaskStatus.PENDING
        return TaskStatus.PLANNING

    async def _has_approved_current_plan(self, task_id: str) -> bool:
        row = await self.db.fetch_one(
            """
            SELECT 1
            FROM plans p
            INNER JOIN tasks t ON t.current_plan_id = p.id
            WHERE t.id = ? AND p.status = ?
            """,
            (task_id, PlanStatus.APPROVED.value),
        )
        return row is not None

    async def _dependency_is_satisfied(self, row: dict[str, Any]) -> bool:
        parent_id = row.get("parent_id")
        if not parent_id:
            return True
        parent = await self.db.fetch_one(
            "SELECT status, deleted_at FROM tasks WHERE id = ?",
            (parent_id,),
        )
        if parent is None or parent.get("deleted_at") is not None:
            return True
        return parent["status"] == TaskStatus.COMPLETED.value

    @staticmethod
    def dependency_blocked_reason(parent_id: str) -> str:
        return f"{DEPENDENCY_BLOCKED_PREFIX} {parent_id} to complete."

    @staticmethod
    def dependency_blocked_resume_instructions(parent_id: str) -> str:
        return (
            f"This task depends on parent task {parent_id}. "
            "Resume automatically once the parent task has completed."
        )

    async def _get_task_row(self, task_id: str) -> dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM tasks WHERE id = ? AND deleted_at IS NULL", (task_id,))
        if row is None:
            raise NotFoundError(f"Task '{task_id}' was not found")
        return row

    async def _decode_task_row(self, row: dict[str, Any]) -> dict[str, Any]:
        row["retry_config"] = json_loads(row.get("retry_config"), None)
        row["metadata"] = json_loads(row.get("metadata"), {})
        row["project_ids"] = await self._get_project_ids(row["id"])
        # current_plan_id is already in the row from the database
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

    async def _resolve_target_session_from_metadata(self, metadata: dict[str, Any]) -> dict[str, Any] | None:
        resolved = await SessionRouteService(self.db).resolve_target_metadata(metadata)
        if resolved is None:
            return None
        return resolved.model_dump(mode="json")

    async def _validate_target_session_metadata(self, connection: Connection, metadata: dict[str, Any]) -> None:
        target_session = metadata.get("target_session")
        if not isinstance(target_session, dict):
            return

        if target_session.get("kind") == "group":
            chat_id = target_session.get("chat_id")
            if chat_id:
                return
            session_key = target_session.get("session_key")
            if not session_key:
                return
            cursor = await connection.execute(
                """
                SELECT id
                FROM session_routes
                WHERE channel = ? AND session_key = ? AND deleted_at IS NULL AND is_active = 1
                """,
                ("whatsapp", session_key),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                raise ConflictError(
                    "Target session_key does not resolve to an active session route. "
                    "Create a session route or provide --target-chat-id."
                )
            return

        if target_session.get("kind") != "dm":
            return

        contact_id = target_session.get("contact_id")
        if contact_id is None:
            return

        cursor = await connection.execute(
            """
            SELECT id, phone_number
            FROM contacts
            WHERE id = ? AND deleted_at IS NULL
            """,
            (str(contact_id),),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise NotFoundError(f"Contact '{contact_id}' was not found")
        if not row["phone_number"]:
            raise ConflictError(f"Contact '{contact_id}' does not have a usable phone number")

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

    async def _add_completion_journal_entry(
        self,
        connection: Connection,
        task_id: str,
        task_title: str,
        result_summary: str | None,
        timestamp: str,
    ) -> None:
        """Add a journal entry to all parent projects when a task is completed."""
        # Get all parent projects for this task
        cursor = await connection.execute(
            """
            SELECT pt.project_id
            FROM project_tasks AS pt
            INNER JOIN projects AS p ON p.id = pt.project_id
            WHERE pt.task_id = ? AND p.deleted_at IS NULL
            """,
            (task_id,),
        )
        project_rows = await cursor.fetchall()
        await cursor.close()

        if not project_rows:
            return

        # Create journal entry content
        content = f"Task completed: {task_title}"
        if result_summary:
            content += f"\n\nResult: {result_summary}"

        # Add journal entry to each parent project
        for row in project_rows:
            project_id = row["project_id"]
            await connection.execute(
                """
                INSERT INTO project_journal_entries (id, project_id, entry_type, content, created_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    project_id,
                    "result",
                    content,
                    timestamp,
                    json_dumps({"task_id": task_id, "task_title": task_title}),
                ),
            )

    async def _trigger_webhook(
        self,
        event: str,
        task_id: str,
        task_title: str,
        result_summary: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Trigger webhook notification for task events.
        
        Extracts session_key from task metadata if present.
        """
        webhook_service = self._get_webhook_service()
        if webhook_service is None:
            return
        
        session_key = metadata.get("session_key") if metadata else None
        project_ids = metadata.get("project_ids", []) if metadata else []
        project_id = project_ids[0] if project_ids else None
        
        try:
            await webhook_service.trigger_event(
                event=event,
                project_id=project_id,
                task_id=task_id,
                task_title=task_title,
                result_summary=result_summary,
                session_key=session_key,
                metadata=metadata,
            )
        except Exception:
            # Don't let webhook failures affect task operations
            pass
