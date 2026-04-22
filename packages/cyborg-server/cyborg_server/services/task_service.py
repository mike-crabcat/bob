"""Business logic for task management."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from aiosqlite import Connection

from cyborg_core.config import Settings
from cyborg_server.database import Database
from cyborg_core.exceptions import ConflictError, NotFoundError
from cyborg_core.models import (
    NotificationEntityType,
    RetryAction,
    RetryConfig,
    TaskBlockRequest,
    TaskCreate,
    TaskFailureRequest,
    TaskFileCreate,
    TaskFileListResponse,
    TaskFilePurpose,
    TaskFileResponse,
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
from cyborg_server.services.base import BaseService, json_dumps, json_loads, next_cron_occurrence, utcnow
from cyborg_server.services.notification_service import NotificationService
from cyborg_server.services.project_autonomy_service import DEPENDENCY_BLOCKED_PREFIX

# Task-level notifications (NEEDS_INPUT, TASK_RESULT) have been removed.
# Only task assignment (reasoning prompt dispatch) remains via NotificationService.
from cyborg_server.services.session_route_service import (
    SessionRouteService,
    has_source_route_metadata,
    merge_source_route_metadata,
)
from cyborg_server.services.webhook_service import WebhookEvent, WebhookService


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

    async def create_task(self, payload: TaskCreate | dict[str, Any]) -> TaskResponse:
        payload = TaskCreate.model_validate(payload)
        now = utcnow()
        task_id = str(uuid4())
        next_run_at = payload.next_run_at
        if payload.is_recurring and payload.recurrence_rule and next_run_at is None:
            next_run_at = next_cron_occurrence(payload.recurrence_rule, now)

        async with self.db.connection(write=True) as connection:
            await self._validate_project_ids(connection, payload.project_ids)
            task_metadata = await self._inherit_project_source_route_metadata(
                connection,
                payload.metadata,
                payload.project_ids,
            )
            if self._require_source_route_metadata() and not has_source_route_metadata(task_metadata):
                raise ConflictError(
                    "Tasks require source routing metadata. "
                    "Provide metadata.channel plus session_key/chat_id, or attach the task to a routed project."
                )
            await self._validate_target_session_metadata(connection, task_metadata)

            dependency_ready = await self._dependency_is_satisfied(
                {"parent_id": str(payload.parent_id) if payload.parent_id else None}
            )
            task_status = TaskStatus.PENDING if dependency_ready else TaskStatus.BLOCKED
            blocked_reason = payload.blocked_reason
            blocked_resume_instructions = payload.blocked_resume_instructions
            if not dependency_ready and payload.parent_id is not None:
                blocked_reason = self.dependency_blocked_reason(str(payload.parent_id))
                blocked_resume_instructions = self.dependency_blocked_resume_instructions(str(payload.parent_id))

            await connection.execute(
                """
                INSERT INTO tasks (
                    id, title, description, requested_by, plan, status, priority,
                    parent_id, current_plan_id, retry_config, is_recurring, recurrence_rule, next_run_at,
                    created_at, updated_at, started_at, completed_at, metadata, deleted_at,
                    blocked_reason, blocked_resume_instructions
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    task_id,
                    payload.title,
                    payload.description,
                    payload.requested_by,
                    payload.plan,
                    task_status.value,
                    payload.priority.value,
                    str(payload.parent_id) if payload.parent_id else None,
                    None,
                    json_dumps(payload.retry_config.model_dump(mode="json")) if payload.retry_config else None,
                    int(payload.is_recurring),
                    payload.recurrence_rule,
                    next_run_at.isoformat() if next_run_at else None,
                    now.isoformat(),
                    now.isoformat(),
                    None,
                    None,
                    json_dumps(task_metadata),
                    blocked_reason,
                    blocked_resume_instructions,
                ),
            )
            await self._replace_project_links(connection, task_id, payload.project_ids)
            await self._add_history(
                connection,
                task_id,
                "created",
                {"status": task_status.value, "priority": payload.priority.value},
                now.isoformat(),
            )
        # Dispatch task assignment as a reasoning prompt (fire-once)
        try:
            await NotificationService(self.db).create_task_assignment_notification(task_id)
        except Exception:
            pass
        return await self.get_task(task_id)

    async def update_task(self, task_id: str, payload: TaskUpdate) -> TaskResponse:
        row = await self._get_task_row(task_id)
        values = payload.model_dump(exclude_unset=True, mode="json")
        project_ids = values.pop("project_ids", None)
        if not values and project_ids is None:
            return await self.get_task(task_id)

        now = utcnow()
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
            if next_status == TaskStatus.ACTIVE and "started_at" not in values:
                values["started_at"] = now.isoformat()
            if next_status == TaskStatus.COMPLETED and "completed_at" not in values:
                values["completed_at"] = now.isoformat()
        elif row["status"] == TaskStatus.BLOCKED.value and (
            "blocked_reason" in values or "blocked_resume_instructions" in values
        ):
            pass

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
        # Trigger project execution flow when status transitions to completed via update
        if values.get("status") == TaskStatus.COMPLETED.value and row["status"] != TaskStatus.COMPLETED.value:
            await self._trigger_project_execution(task_id, row["title"])
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

    async def submit_task(self, task_id: str, result_summary: str | None = None) -> TaskResponse:
        """Submit a task for review. OpenClaw agents must use this instead of complete.

        Sets status to SUBMITTED, auto-registers output files, generates a one-time
        password for verification, and dispatches a submission review notification
        to the agent session. The agent then calls verify_submit with the OTP.
        """
        import secrets

        row = await self._get_task_row(task_id)
        if row["status"] != TaskStatus.ACTIVE.value:
            raise ConflictError(
                f"Cannot submit task '{task_id}' from '{row['status']}'. Task must be 'active'."
            )

        now = utcnow()
        otp = secrets.token_urlsafe(24)

        # 1. Transition to SUBMITTED and store OTP
        async with self.db.connection(write=True) as connection:
            await connection.execute(
                """
                UPDATE tasks
                SET status = ?, result = COALESCE(?, result), submitted_at = ?, updated_at = ?,
                    submission_review_otp = ?
                WHERE id = ? AND deleted_at IS NULL
                """,
                (
                    TaskStatus.SUBMITTED.value,
                    result_summary,
                    now.isoformat(),
                    now.isoformat(),
                    otp,
                    task_id,
                ),
            )
            await self._add_history(
                connection,
                task_id,
                "submitted",
                {"result_summary": result_summary},
                now.isoformat(),
            )

        # 2. Auto-register untracked output files
        output_directory = await self._compute_output_directory(task_id)
        if output_directory:
            try:
                await self._auto_register_untracked_files(task_id, output_directory)
            except Exception:
                pass

        # 3. Dispatch submission review notification to the agent session
        try:
            await NotificationService(self.db).create_submission_review_notification(
                task_id, otp, now=now,
            )
        except Exception:
            pass

        return await self.get_task(task_id)

    async def verify_submit(self, task_id: str, payload: TaskVerifySubmitRequest) -> TaskResponse:
        """Verify a task submission using the one-time password.

        Called by the agent after reviewing the task work. The OTP must match
        the one generated during submit_task.
        """
        from cyborg_core.models import TaskVerifySubmitRequest as _  # noqa: F811 — already imported

        row = await self._get_task_row(task_id)
        if row["status"] != TaskStatus.SUBMITTED.value:
            raise ConflictError(
                f"Cannot verify task '{task_id}' from '{row['status']}'. Task must be 'submitted'."
            )

        stored_otp = row.get("submission_review_otp")
        if not stored_otp or payload.otp != stored_otp:
            raise ConflictError("Invalid or expired verification code")

        # Clear the OTP (one-time use)
        now = utcnow()
        async with self.db.connection(write=True) as connection:
            await connection.execute(
                "UPDATE tasks SET submission_review_otp = NULL, updated_at = ? WHERE id = ?",
                (now.isoformat(), task_id),
            )

        if payload.approved:
            result_summary = row.get("result")
            return await self.complete_task(task_id, result_summary)

        # Rejected - send back to active and notify
        review = {
            "approved": False,
            "reasoning": payload.reason or "Submission rejected by reviewer",
            "issues": payload.issues or [],
            "suggestions": [],
        }
        async with self.db.connection(write=True) as connection:
            await connection.execute(
                """
                UPDATE tasks
                SET status = ?, submitted_at = NULL, updated_at = ?
                WHERE id = ? AND deleted_at IS NULL
                """,
                (
                    TaskStatus.ACTIVE.value,
                    now.isoformat(),
                    task_id,
                ),
            )
            await self._add_history(
                connection,
                task_id,
                "submission_rejected",
                {
                    "reasoning": review["reasoning"],
                    "issues": review["issues"],
                },
                now.isoformat(),
            )

        # Dispatch retry notification
        try:
            await NotificationService(self.db).create_task_retry_notification(
                task_id, review, now=now
            )
        except Exception:
            pass

        return await self.get_task(task_id)

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

        task_response = await self.get_task(task_id)

        # Resolve any pending/failed-delivery notifications for this task so
        # the retry loop won't re-dispatch stale prompts to a completed task.
        try:
            notification_service = NotificationService(self.db)
            await notification_service._resolve_pending_notifications(
                NotificationEntityType.TASK, task_id, now=now,
            )
        except Exception:
            pass

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
            from cyborg_server.services.project_autonomy_service import ProjectAutonomyService

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

    async def block_task(self, task_id: str, payload: TaskBlockRequest) -> TaskResponse:
        """Block a task waiting for user input.

        Records the reason and full resume instructions so the task can be
        resumed without relying on conversational context.

        Always creates a task_input approval record so the dashboard can
        display the blocked task. When input_schema is provided the approval
        includes a structured input form; otherwise it shows an Unblock button.
        """
        row = await self._get_task_row(task_id)
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
                {"reason": payload.reason, "has_input_schema": payload.input_schema is not None},
                now.isoformat(),
            )

        await self._create_task_input_approval(task_id, row, payload, now)

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

        if status == TaskStatus.ACTIVE:
            if row["status"] != TaskStatus.PENDING.value:
                raise ConflictError(
                    f"Cannot transition task '{task_id}' from '{row['status']}' to 'active'. "
                    "Task must be in 'pending' status."
                )

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

    async def _validate_status_transition(self, task_id: str, current_status: str, next_status: TaskStatus) -> None:
        if current_status == next_status.value:
            return
        if next_status == TaskStatus.PENDING:
            row = await self._get_task_row(task_id)
            if not await self._dependency_is_satisfied(row):
                raise ConflictError(
                    f"Cannot transition task '{task_id}' to 'pending' while its dependency is incomplete."
                )
            return
        if next_status == TaskStatus.ACTIVE:
            if current_status not in (TaskStatus.PENDING.value, TaskStatus.SUBMITTED.value):
                raise ConflictError(
                    f"Cannot transition task '{task_id}' from '{current_status}' to 'active'. "
                    "Task must be in 'pending' or 'submitted' status."
                )
            row = await self._get_task_row(task_id)
            if not await self._dependency_is_satisfied(row):
                raise ConflictError(
                    f"Cannot transition task '{task_id}' to 'active' while its dependency is incomplete."
                )
            return
        if next_status == TaskStatus.SUBMITTED:
            if current_status != TaskStatus.ACTIVE.value:
                raise ConflictError(
                    f"Cannot transition task '{task_id}' from '{current_status}' to 'submitted'. "
                    "Task must be in 'active' status."
                )
            return
        if next_status == TaskStatus.COMPLETED:
            if current_status not in (TaskStatus.ACTIVE.value, TaskStatus.SUBMITTED.value):
                raise ConflictError(
                    f"Cannot transition task '{task_id}' from '{current_status}' to 'completed'. "
                    "Task must be in 'active' or 'submitted' status."
                )

    async def _determine_unblocked_status(self, task_id: str, row: dict[str, Any]) -> TaskStatus:
        if not await self._dependency_is_satisfied(row):
            return TaskStatus.BLOCKED
        if row.get("started_at"):
            return TaskStatus.ACTIVE
        return TaskStatus.PENDING

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

    async def _create_task_input_approval(
        self,
        task_id: str,
        task_row: dict[str, Any],
        payload: TaskBlockRequest,
        now: Any,
    ) -> None:
        """Create a task_input approval record so the dashboard can collect user input."""
        from uuid import uuid4

        approval_id = str(uuid4())
        now_iso = now.isoformat()
        input_schema_json = json_dumps(payload.input_schema.model_dump(mode="json")) if payload.input_schema else None

        proposal_data = {
            "task_id": task_id,
            "task_title": task_row.get("title", ""),
            "reason": payload.reason,
            "resume_instructions": payload.resume_instructions,
        }

        await self.db.execute(
            """
            INSERT INTO approvals (
                id, approval_type, entity_id, title, description,
                proposal_data, status, priority, requested_at, requested_by,
                input_schema, created_at
            ) VALUES (?, 'task_input', ?, ?, ?, ?, 'pending', 'normal', ?, 'system', ?, ?)
            """,
            (
                approval_id,
                task_id,
                f"Task input needed: {task_row.get('title', 'Task')}",
                payload.reason,
                json_dumps(proposal_data),
                now_iso,
                input_schema_json,
                now_iso,
            ),
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
        row.pop("current_plan_id", None)
        row.pop("submission_review_otp", None)
        # Attach output directory and file listings
        row["output_directory"] = await self._compute_output_directory(row["id"])
        row["files"] = await self._fetch_task_file_dicts(row["id"])
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

    async def _inherit_project_source_route_metadata(
        self,
        connection: Connection,
        metadata: dict[str, Any],
        project_ids: list[Any],
    ) -> dict[str, Any]:
        merged_metadata = dict(metadata or {})
        if has_source_route_metadata(merged_metadata):
            return merged_metadata

        unique_project_ids = list(dict.fromkeys(str(project_id) for project_id in project_ids))
        for project_id in unique_project_ids:
            project = await self._fetch_one_connection(
                connection,
                "SELECT metadata FROM projects WHERE id = ? AND deleted_at IS NULL",
                (project_id,),
            )
            if project is None:
                continue
            merged_metadata = merge_source_route_metadata(
                merged_metadata,
                json_loads(project.get("metadata"), {}),
            )
            if has_source_route_metadata(merged_metadata):
                return merged_metadata
        return merged_metadata

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

    def _require_source_route_metadata(self) -> bool:
        current = getattr(self.db, "settings", None)
        if isinstance(current, Settings):
            return current.openclaw.enabled
        return False

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

    # ------------------------------------------------------------------
    # File management
    # ------------------------------------------------------------------

    async def register_task_file(
        self,
        task_id: str,
        project_id: str,
        payload: TaskFileCreate,
    ) -> TaskFileResponse:
        """Register a file produced by a task in the database."""
        await self._get_task_row(task_id)
        now = utcnow().isoformat()

        # Compute the relative path under the project workspace
        relative_path = f"tasks/{task_id.replace('-', '')[:8]}/{payload.filename}"

        # Determine size if possible
        size_bytes: int | None = None
        try:
            from pathlib import Path

            full_path = Path(f"/home/mike/.openclaw/workspace/projects") / relative_path
            if full_path.exists():
                size_bytes = full_path.stat().st_size
        except Exception:
            pass

        file_id = str(uuid4())
        await self.db.execute(
            """
            INSERT INTO task_files (
                id, task_id, project_id, filename, relative_path,
                purpose, description, content_type, size_bytes,
                metadata, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_id,
                task_id,
                project_id,
                payload.filename,
                relative_path,
                payload.purpose.value,
                payload.description,
                payload.content_type,
                size_bytes,
                json_dumps({}),
                now,
                now,
            ),
        )
        row = await self.db.fetch_one("SELECT * FROM task_files WHERE id = ?", (file_id,))
        return self._task_file_response_from_row(dict(row))

    async def list_task_files(self, task_id: str) -> TaskFileListResponse:
        """Return all files registered for a task, plus the output directory."""
        await self._get_task_row(task_id)
        rows = await self.db.fetch_all(
            "SELECT * FROM task_files WHERE task_id = ? ORDER BY created_at ASC",
            (task_id,),
        )
        files = [self._task_file_response_from_row(dict(r)) for r in rows]
        return TaskFileListResponse(
            task_id=task_id,
            output_directory=await self._compute_output_directory(task_id),
            files=files,
        )

    async def delete_task_file(self, task_id: str, file_id: str) -> None:
        """Remove a task-file record from the database (does NOT delete from disk)."""
        await self._get_task_row(task_id)
        await self.db.execute(
            "DELETE FROM task_files WHERE id = ? AND task_id = ?",
            (file_id, task_id),
        )

    async def _compute_output_directory(self, task_id: str) -> str | None:
        """Return the output directory path for a task, or None if no project link."""
        project_ids = await self._get_project_ids(task_id)
        if not project_ids:
            return None
        project_id = project_ids[0]
        try:
            from cyborg_server.services.project_service import ProjectService

            project_service = ProjectService(self.db)
            project_path = await project_service.get_project_path(project_id)
            short_id = task_id.replace("-", "")[:8]
            return str(project_path / "tasks" / short_id)
        except Exception:
            return None

    async def _fetch_task_file_dicts(self, task_id: str) -> list[dict[str, Any]]:
        """Fetch task file rows as plain dicts suitable for TaskFileResponse validation."""
        rows = await self.db.fetch_all(
            "SELECT * FROM task_files WHERE task_id = ? ORDER BY created_at ASC",
            (task_id,),
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            d = dict(row)
            d["metadata"] = json_loads(d.get("metadata"), {})
            result.append(d)
        return result

    async def _auto_register_untracked_files(self, task_id: str, output_directory: str) -> None:
        """Scan the output directory for files not yet tracked and register them."""
        from pathlib import Path

        output_path = Path(output_directory)
        if not output_path.exists():
            return

        project_ids = await self._get_project_ids(task_id)
        if not project_ids:
            return
        project_id = project_ids[0]

        # Get already-tracked filenames
        tracked = await self.db.fetch_all(
            "SELECT filename FROM task_files WHERE task_id = ?",
            (task_id,),
        )
        tracked_names = {r["filename"] for r in tracked}

        for child in sorted(output_path.iterdir()):
            if child.is_dir():
                continue
            if child.name in tracked_names:
                continue
            payload = TaskFileCreate(
                filename=child.name,
                purpose=self._guess_purpose(child.name),
                content_type=self._guess_content_type(child.name),
            )
            try:
                await self.register_task_file(task_id, project_id, payload)
            except Exception:
                pass

    @staticmethod
    def _guess_purpose(filename: str) -> TaskFilePurpose:
        """Guess a file purpose from its name."""
        name = filename.lower()
        if name.endswith((".log",)):
            return TaskFilePurpose.LOG
        if name.endswith((".json", ".csv", ".yaml", ".yml", ".toml")):
            return TaskFilePurpose.ANALYSIS
        if name.endswith((".md", ".txt", ".rst")):
            return TaskFilePurpose.RESULT
        return TaskFilePurpose.ARTIFACT

    @staticmethod
    def _guess_content_type(filename: str) -> str:
        """Guess MIME type from filename extension."""
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        mapping = {
            "json": "application/json",
            "csv": "text/csv",
            "md": "text/markdown",
            "txt": "text/plain",
            "html": "text/html",
            "yaml": "text/yaml",
            "yml": "text/yaml",
            "toml": "text/plain",
            "log": "text/plain",
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "pdf": "application/pdf",
        }
        return mapping.get(ext, "application/octet-stream")

    @staticmethod
    def _task_file_response_from_row(row: dict[str, Any]) -> TaskFileResponse:
        """Build a TaskFileResponse from a raw DB row dict."""
        row["metadata"] = json_loads(row.get("metadata"), {})
        return TaskFileResponse.model_validate(row)
