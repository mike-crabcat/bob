"""Business rules for persisted client notifications.

Simplified: only project-level notifications for COMPLETED and BLOCKED states.
Task assignments are dispatched as reasoning prompts at task creation time.
No periodic scanning or repeat logic - all notifications fire once.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from aiosqlite import Connection

from cyborg_server.context import AppContext
from cyborg_server.database import Database
from cyborg_server.exceptions import NotFoundError
from cyborg_server.models import (
    NotificationAcknowledgeRequest,
    NotificationDeliveryStatus,
    NotificationEntityType,
    NotificationResponse,
    NotificationStatus,
    NotificationType,
    ProjectState,
)
from cyborg_server.services.base import BaseService, json_dumps, json_loads, utcnow
from cyborg_server.services.openclaw_hook_service import OpenClawHookService


# ── Shared dispatch metadata helpers ────────────────────────────


async def build_task_dispatch_context(db: Database, task_id: str) -> dict[str, Any] | None:
    """Build the common metadata dict for a task dispatch.

    Returns None if the task has no delivery route (nothing to dispatch to).
    """
    row = await db.fetch_one(
        "SELECT * FROM tasks WHERE id = ? AND deleted_at IS NULL",
        (task_id,),
    )
    if row is None:
        return None

    task_metadata = json_loads(row.get("metadata"), {})
    target_session = task_metadata.get("target_session")
    has_source_route = any(task_metadata.get(field) for field in ("session_key", "chat_id", "channel"))
    delivery_route = "target" if isinstance(target_session, dict) else ("source" if has_source_route else None)

    if delivery_route is None:
        return None

    parent_project = await _get_parent_project(db, task_id)

    output_directory = None
    if parent_project is not None:
        try:
            from cyborg_server.context import AppContext
            from cyborg_server.services.project_service import ProjectService

            ctx = AppContext(db=db, settings=db.get_settings())
            project_service = ProjectService(ctx)
            project_path = await project_service.get_project_path(parent_project["id"])
            short_id = row["id"].replace("-", "")[:8]
            output_directory = str(project_path / "tasks" / short_id)
        except Exception:
            pass

    return {
        "task_row": dict(row),
        "task_metadata": {
            **task_metadata,
            "task_id": row["id"],
            "task_status": row["status"],
            "parent_project_id": parent_project["id"] if parent_project else None,
            "parent_project_title": parent_project["title"] if parent_project else None,
            "delivery_route": delivery_route,
            "output_directory": output_directory,
        },
    }


async def _get_parent_project(db: Database, task_id: str) -> dict[str, Any] | None:
    return await db.fetch_one(
        """
        SELECT p.id, p.title
        FROM project_tasks AS pt
        INNER JOIN projects AS p ON p.id = pt.project_id
        WHERE pt.task_id = ? AND p.deleted_at IS NULL
        ORDER BY p.created_at ASC, p.id ASC
        LIMIT 1
        """,
        (task_id,),
    )


class NotificationService(BaseService):
    """Create, acknowledge, resolve, and dispatch persisted notifications.

    Only two notification scenarios remain:
    - PROJECT_RESULT: when a project is completed (auto or manual close)
    - NEEDS_INPUT: when a project is blocked (PAUSED + blocked_reason)

    Both fire exactly once. No repeat logic or periodic scanning.
    """

    def __init__(self, ctx: AppContext, openclaw_service: OpenClawHookService | None = None) -> None:
        super().__init__(ctx)
        self._openclaw_service = openclaw_service

    def _get_openclaw_service(self) -> OpenClawHookService:
        if self._openclaw_service is None:
            settings = self._get_settings()
            self._openclaw_service = OpenClawHookService(self.ctx, cyborg_service_url=settings.resolved_public_url)
        return self._openclaw_service

    # ── Public API ──────────────────────────────────────────────

    async def list_notifications(
        self,
        *,
        status: NotificationStatus | None = NotificationStatus.PENDING,
        entity_type: NotificationEntityType | None = None,
        limit: int = 100,
    ) -> list[NotificationResponse]:
        query = "SELECT * FROM notifications WHERE 1 = 1"
        params: list[Any] = []
        if status is not None:
            query += " AND status = ?"
            params.append(status.value)
        if entity_type is not None:
            query += " AND entity_type = ?"
            params.append(entity_type.value)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = await self.db.fetch_all(query, tuple(params))
        return [NotificationResponse.model_validate(self._decode_notification_row(row)) for row in rows]

    async def get_notification(self, notification_id: str) -> NotificationResponse:
        row = await self._get_notification_row(notification_id)
        return NotificationResponse.model_validate(self._decode_notification_row(row))

    async def acknowledge_notification(
        self,
        notification_id: str,
        payload: NotificationAcknowledgeRequest,
    ) -> NotificationResponse:
        row = await self._get_notification_row(notification_id)
        if row["status"] == NotificationStatus.ACKNOWLEDGED.value:
            return NotificationResponse.model_validate(self._decode_notification_row(row))
        if row["status"] != NotificationStatus.PENDING.value:
            return NotificationResponse.model_validate(self._decode_notification_row(row))

        now = utcnow().isoformat()
        await self.db.execute(
            """
            UPDATE notifications
            SET status = ?, acknowledged_at = ?, acknowledged_by = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                NotificationStatus.ACKNOWLEDGED.value,
                now,
                payload.acknowledged_by,
                now,
                notification_id,
            ),
        )
        return await self.get_notification(notification_id)

    async def dispatch_pending(self, *, now: datetime | None = None) -> int:
        """Dispatch pending notifications whose delivery has not yet succeeded."""
        return await self._dispatch_due_notifications(now or utcnow())

    # ── Project-level notification sync (fire-once) ─────────────

    async def sync_project_state(
        self,
        project_id: str,
        *,
        now: datetime | None = None,
    ) -> None:
        """Create or resolve project NEEDS_INPUT notification based on current state.

        Fire-once: only creates a notification if none is already pending.
        Only triggers for PAUSED projects with a blocked_reason.
        """
        reference = now or utcnow()
        row = await self.db.fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,))
        if row is None:
            await self._resolve_pending_notifications(
                NotificationEntityType.PROJECT, project_id, now=reference,
            )
            return

        # Only PAUSED + blocked_reason triggers NEEDS_INPUT
        if row.get("deleted_at") is not None or row["state"] != ProjectState.PAUSED.value or not row.get("blocked_reason"):
            await self._resolve_pending_notifications(
                NotificationEntityType.PROJECT, project_id, now=reference,
                notification_types={NotificationType.NEEDS_INPUT},
            )
            return

        # Fire-once: skip if any NEEDS_INPUT notification exists (pending or acknowledged)
        existing = await self.db.fetch_one(
            """
            SELECT id FROM notifications
            WHERE entity_type = ? AND entity_id = ? AND notification_type = ?
              AND status IN (?, ?)
            LIMIT 1
            """,
            (
                NotificationEntityType.PROJECT.value,
                project_id,
                NotificationType.NEEDS_INPUT.value,
                NotificationStatus.PENDING.value,
                NotificationStatus.ACKNOWLEDGED.value,
            ),
        )
        if existing is not None:
            return

        await self._create_project_input_notification(row, reference)

    async def create_project_result_notification(
        self,
        project_id: str,
        *,
        conclusion: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Create a PROJECT_RESULT notification when a project is completed."""
        row = await self.db.fetch_one("SELECT * FROM projects WHERE id = ? AND deleted_at IS NULL", (project_id,))
        if row is None:
            return
        project_metadata = json_loads(row.get("metadata"), {})
        message = conclusion or row.get("conclusion") or "The project completed."
        await self._create_entity_notification(
            entity_type=NotificationEntityType.PROJECT,
            entity_id=row["id"],
            notification_type=NotificationType.PROJECT_RESULT,
            title=f"Project completed: {row['title']}",
            message=message,
            metadata={
                **project_metadata,
                "project_id": row["id"],
                "project_state": row["state"],
                "delivery_route": "source",
            },
            now=now or utcnow(),
            source_updated_at=row.get("closed_at") or row.get("updated_at"),
        )

    # ── Task assignment (dispatches through dispatch system) ─────

    async def create_task_assignment_notification(
        self,
        task_id: str,
        *,
        now: datetime | None = None,
    ) -> None:
        """Dispatch a TASK_ASSIGNMENT prompt to OpenClaw via the dispatch system."""
        ctx = await build_task_dispatch_context(self.db, task_id)
        if ctx is None:
            return
        metadata = ctx["task_metadata"]
        await self._dispatch_agent_via_dispatch(
            notification_type=NotificationType.TASK_ASSIGNMENT,
            title=f"Task to action: {ctx['task_row']['title']}",
            message=self._build_task_assignment_message(ctx),
            metadata=metadata,
            entity_id=task_id,
            task_id=task_id,
            project_id=metadata.get("parent_project_id"),
        )

    # ── Task retry (dispatches through dispatch system) ──────────

    async def create_task_retry_notification(
        self,
        task_id: str,
        review_feedback: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> None:
        """Dispatch a TASK_RETRY prompt to OpenClaw via the dispatch system."""
        ctx = await build_task_dispatch_context(self.db, task_id)
        if ctx is None:
            return
        metadata = {**ctx["task_metadata"], "review_feedback": review_feedback}
        issues = review_feedback.get("issues", [])
        suggestions = review_feedback.get("suggestions", [])
        reasoning = review_feedback.get("reasoning", "")
        message_parts = [f"Task submission rejected: {ctx['task_row']['title']}", "", f"Reason: {reasoning}"]
        if issues:
            message_parts += ["", "Issues:"] + [f"  - {i}" for i in issues]
        if suggestions:
            message_parts += ["", "Suggestions:"] + [f"  - {s}" for s in suggestions]
        await self._dispatch_agent_via_dispatch(
            notification_type=NotificationType.TASK_RETRY,
            title=f"Task retry: {ctx['task_row']['title']}",
            message="\n".join(message_parts),
            metadata=metadata,
            entity_id=task_id,
            task_id=task_id,
            project_id=ctx["task_metadata"].get("parent_project_id"),
        )

    # ── Task input response (dispatches through dispatch system) ─

    async def create_task_input_response_notification(
        self,
        task_id: str,
        input_response: str | list[str],
        input_prompt: str,
        approval_id: str,
        *,
        now: datetime | None = None,
    ) -> None:
        """Dispatch a TASK_INPUT_RESPONSE prompt to OpenClaw via the dispatch system."""
        ctx = await build_task_dispatch_context(self.db, task_id)
        if ctx is None:
            return
        response_text = ", ".join(input_response) if isinstance(input_response, list) else input_response
        metadata = {**ctx["task_metadata"], "input_response": input_response, "input_prompt": input_prompt, "approval_id": approval_id}
        await self._dispatch_agent_via_dispatch(
            notification_type=NotificationType.TASK_INPUT_RESPONSE,
            title=f"Task input received: {ctx['task_row']['title']}",
            message=f"Question: {input_prompt}\n\nUser response: {response_text}",
            metadata=metadata,
            entity_id=task_id,
            task_id=task_id,
            project_id=ctx["task_metadata"].get("parent_project_id"),
        )

    # ── Task tap (operator nudge) ───────────────────────────────────

    async def create_task_tap_notification(
        self,
        task_id: str,
        *,
        now: datetime | None = None,
    ) -> str | None:
        """Dispatch a TASK_TAP prompt to OpenClaw via the dispatch system."""
        ctx = await build_task_dispatch_context(self.db, task_id)
        if ctx is None:
            return None
        metadata = ctx["task_metadata"]
        title = f"Task tap: {ctx['task_row']['title']}"
        message = f"Status check on active task: {ctx['task_row']['title']}"
        await self._dispatch_agent_via_dispatch(
            notification_type=NotificationType.TASK_TAP,
            title=title,
            message=message,
            metadata=metadata,
            entity_id=task_id,
            task_id=task_id,
            project_id=metadata.get("parent_project_id"),
        )
        return None

    # ── Submission review ─────────────────────────────────────────

    async def create_submission_review_notification(
        self,
        task_id: str,
        otp: str,
        *,
        now: datetime | None = None,
    ) -> str | None:
        """Dispatch a SUBMISSION_REVIEW prompt to OpenClaw via the dispatch system."""
        ctx = await build_task_dispatch_context(self.db, task_id)
        if ctx is None:
            return None
        metadata = {
            **ctx["task_metadata"],
            "submission_review_otp": otp,
            "result_summary": ctx["task_row"].get("result"),
        }
        await self._dispatch_agent_via_dispatch(
            notification_type=NotificationType.SUBMISSION_REVIEW,
            title=f"Review submission: {ctx['task_row']['title']}",
            message=f"Task submitted for review: {ctx['task_row']['title']}",
            metadata=metadata,
            entity_id=task_id,
            task_id=task_id,
            project_id=ctx["task_metadata"].get("parent_project_id"),
        )
        return None

    # ── Next action (dispatches through dispatch system) ─────────

    async def create_next_action_notification(
        self,
        project_id: str,
        prompt: str,
        otp: str,
        completed_task_id: str,
        *,
        now: datetime | None = None,
    ) -> None:
        """Dispatch a NEXT_ACTION prompt to OpenClaw via the dispatch system."""
        project = await self.db.fetch_one(
            "SELECT id, title, metadata, updated_at, subagent_session_key FROM projects WHERE id = ? AND deleted_at IS NULL",
            (project_id,),
        )
        if project is None:
            return

        project_metadata = json_loads(project.get("metadata"), {})

        metadata = {
            **project_metadata,
            "project_id": project_id,
            "completed_task_id": completed_task_id,
            "delivery_route": "source",
            "next_action_otp": otp,
            "subagent_session_key": project.get("subagent_session_key"),
        }

        await self._dispatch_agent_via_dispatch(
            notification_type=NotificationType.NEXT_ACTION,
            title=f"Decide next action: {project['title']}",
            message=prompt,
            metadata=metadata,
            entity_id=project_id,
            project_id=project_id,
        )

    # ── Backward-compat shim for process_due_notifications ──────

    async def process_due_notifications(self, *, now: datetime | None = None) -> int:
        """Dispatch any pending notifications. Kept for API compatibility."""
        return await self.dispatch_pending(now=now or utcnow())

    # ── Internal: project NEEDS_INPUT ───────────────────────────

    async def _create_project_input_notification(self, row: dict[str, Any], now: datetime) -> None:
        """Create NEEDS_INPUT notification for a blocked project."""
        project_metadata = json_loads(row.get("metadata"), {})
        title = f"Project needs input: {row['title']}"
        message = row["blocked_reason"]
        if row.get("blocked_resume_instructions"):
            message += f"\n\nResume instructions: {row['blocked_resume_instructions']}"

        metadata = {
            **project_metadata,
            "project_id": row["id"],
            "project_state": row["state"],
            "blocked_reason": row.get("blocked_reason"),
            "blocked_resume_instructions": row.get("blocked_resume_instructions"),
            "delivery_route": "source",
        }
        await self._create_entity_notification(
            entity_type=NotificationEntityType.PROJECT,
            entity_id=row["id"],
            notification_type=NotificationType.NEEDS_INPUT,
            title=title,
            message=message,
            metadata=metadata,
            now=now,
            source_updated_at=row.get("paused_at") or row.get("updated_at"),
        )

    # ── Internal: notification lifecycle ────────────────────────

    async def _create_entity_notification(
        self,
        *,
        entity_type: NotificationEntityType,
        entity_id: str,
        notification_type: NotificationType,
        title: str,
        message: str,
        metadata: dict[str, Any],
        now: datetime,
        source_updated_at: str | None,
    ) -> None:
        notification_id = str(uuid4())
        now_iso = now.isoformat()
        sequence_number: int | None = None

        async with self.db.connection(write=True) as connection:
            if entity_type in {NotificationEntityType.TASK, NotificationEntityType.PROJECT}:
                sequence_number = await self._increment_entity_notification_stats(connection, entity_type, entity_id, now_iso)

            await connection.execute(
                """
                INSERT INTO notifications (
                    id, entity_type, entity_id, notification_type, status,
                    delivery_status, delivery_attempt_count, last_delivery_at, last_delivery_error, next_delivery_at,
                    title, message, metadata, sequence_number,
                    created_at, updated_at, acknowledged_at, acknowledged_by, resolved_at, source_updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?)
                """,
                (
                    notification_id,
                    entity_type.value,
                    entity_id,
                    notification_type.value,
                    NotificationStatus.PENDING.value,
                    NotificationDeliveryStatus.PENDING.value,
                    now_iso,
                    title,
                    message,
                    json_dumps(metadata),
                    sequence_number,
                    now_iso,
                    now_iso,
                    source_updated_at,
                ),
            )

        # Skip dispatch if the parent project is muted
        muted_project_id = None
        if entity_type == NotificationEntityType.PROJECT:
            muted_project_id = entity_id
        elif entity_type == NotificationEntityType.TASK:
            parent = await self._get_parent_project(entity_id)
            muted_project_id = parent["id"] if parent else None

        if muted_project_id and await self._is_project_muted(muted_project_id):
            return

        await self._attempt_dispatch_notification(notification_id, now=now)

    async def _increment_entity_notification_stats(
        self,
        connection: Connection,
        entity_type: NotificationEntityType,
        entity_id: str,
        timestamp: str,
    ) -> int:
        table = "tasks" if entity_type == NotificationEntityType.TASK else "projects"
        cursor = await connection.execute(
            f"SELECT notification_count FROM {table} WHERE id = ?",
            (entity_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise NotFoundError(f"{entity_type.value.capitalize()} '{entity_id}' was not found")

        next_count = int(row["notification_count"] or 0) + 1
        update_fields = ["notification_count = ?", "last_notification_at = ?"]
        update_params: list[Any] = [next_count, timestamp]
        if table == "projects":
            update_fields.append("updated_at = ?")
            update_params.append(timestamp)
        update_params.append(entity_id)
        await connection.execute(
            f"UPDATE {table} SET {', '.join(update_fields)} WHERE id = ?",
            tuple(update_params),
        )
        return next_count

    async def _resolve_pending_notifications(
        self,
        entity_type: NotificationEntityType,
        entity_id: str,
        *,
        now: datetime,
        notification_types: set[NotificationType] | None = None,
    ) -> None:
        query = """
            UPDATE notifications
            SET status = ?, resolved_at = ?, updated_at = ?
            WHERE entity_type = ? AND entity_id = ? AND status = ?
        """
        params: list[Any] = [
            NotificationStatus.RESOLVED.value,
            now.isoformat(),
            now.isoformat(),
            entity_type.value,
            entity_id,
            NotificationStatus.PENDING.value,
        ]
        if notification_types:
            placeholders = ", ".join("?" for _ in notification_types)
            query += f" AND notification_type IN ({placeholders})"
            params.extend(notification_type.value for notification_type in sorted(notification_types, key=lambda value: value.value))
        await self.db.execute(query, tuple(params))

        # Also resolve any failed-delivery notifications so retry won't re-dispatch
        # to completed/closed entities.
        failed_query = """
            UPDATE notifications
            SET status = ?, resolved_at = ?, updated_at = ?
            WHERE entity_type = ? AND entity_id = ?
              AND status = ? AND delivery_status = ?
        """
        failed_params: list[Any] = [
            NotificationStatus.RESOLVED.value,
            now.isoformat(),
            now.isoformat(),
            entity_type.value,
            entity_id,
            NotificationStatus.PENDING.value,
            NotificationDeliveryStatus.FAILED.value,
        ]
        if notification_types:
            placeholders = ", ".join("?" for _ in notification_types)
            failed_query += f" AND notification_type IN ({placeholders})"
            failed_params.extend(notification_type.value for notification_type in sorted(notification_types, key=lambda value: value.value))
        await self.db.execute(failed_query, tuple(failed_params))

    # ── Internal: agent dispatch via dispatch system ─────────────

    async def _dispatch_agent_via_dispatch(
        self,
        *,
        notification_type: NotificationType,
        title: str,
        message: str,
        metadata: dict[str, Any],
        entity_id: str,
        task_id: str | None = None,
        project_id: str | None = None,
    ) -> None:
        """Send an agent dispatch through the dispatch system with track().

        Skips the notifications table entirely. Records a dispatch, builds
        the prompt via openclaw_hook_service, and tracks completion.
        """
        from cyborg_server.services.dispatch_service import DispatchService

        openclaw_service = self._get_openclaw_service()
        if not openclaw_service.is_configured():
            return

        notification_dict = {
            "id": str(uuid4()),
            "entity_type": "task" if task_id else "project",
            "entity_id": entity_id,
            "notification_type": notification_type.value,
            "title": title,
            "message": message,
            "metadata": metadata,
        }

        session_key = await self._resolve_agent_session_key(openclaw_service, notification_dict, metadata)
        if not session_key:
            return

        from cyborg_server.services.prompt_history import log_prompt
        await log_prompt(
            self.db,
            category=notification_type.value,
            prompt_text=message,
            project_id=project_id,
            task_id=task_id,
            session_key=session_key,
        )

        dispatch_id = await DispatchService(self.ctx).record_dispatch(
            notification_type=notification_type.value,
            session_key=session_key,
            task_id=task_id,
            project_id=project_id,
        )

        coro = await openclaw_service.prepare_agent_dispatch(
            message=message,
            session_key=session_key,
            idempotency_key=notification_dict["id"],
        )
        DispatchService(self.ctx).track(dispatch_id, coro)

    async def _resolve_agent_session_key(
        self,
        openclaw_service: OpenClawHookService,
        notification_dict: dict[str, Any],
        metadata: dict[str, Any],
    ) -> str | None:
        """Resolve the session key for an agent dispatch."""
        session_key = metadata.get("subagent_session_key")
        if session_key:
            return session_key

        session_key = await openclaw_service.resolve_project_session_key(
            metadata.get("parent_project_id") or metadata.get("project_id", "")
        )
        if session_key:
            return session_key

        route = await openclaw_service.routing_service.resolve_notification_route(metadata)
        if route:
            route_data = route.model_dump(mode="json")
            sk = route_data.get("session_key")
            if sk:
                return sk

        return None

    @staticmethod
    def _build_task_assignment_message(ctx: dict[str, Any]) -> str:
        row = ctx["task_row"]
        message_parts = []
        if row.get("description"):
            message_parts.append(row["description"])
        if row.get("requested_by"):
            message_parts.append(f"Requested by: {row['requested_by']}")
        parent_project_id = ctx["task_metadata"].get("parent_project_id")
        parent_project_title = ctx["task_metadata"].get("parent_project_title")
        if parent_project_id:
            message_parts.append(f"Project: {parent_project_title} ({parent_project_id})")
        return "\n\n".join(part for part in message_parts if part) or "A task is ready to action."

    # ── Internal: notification dispatch ──────────────────────────

    async def _dispatch_due_notifications(self, now: datetime) -> int:
        openclaw_service = self._get_openclaw_service()
        if not openclaw_service.is_configured():
            return 0

        rows = await self.db.fetch_all(
            """
            SELECT id
            FROM notifications
            WHERE status = ? AND delivery_status IN (?, ?)
              AND (next_delivery_at IS NULL OR next_delivery_at <= ?)
            ORDER BY created_at ASC
            """,
            (
                NotificationStatus.PENDING.value,
                NotificationDeliveryStatus.PENDING.value,
                NotificationDeliveryStatus.FAILED.value,
                now.isoformat(),
            ),
        )
        count = 0
        for row in rows:
            if await self._attempt_dispatch_notification(row["id"], now=now):
                count += 1
        return count

    async def _attempt_dispatch_notification(self, notification_id: str, *, now: datetime) -> bool:
        openclaw_service = self._get_openclaw_service()
        if not openclaw_service.is_configured():
            return False

        row = await self._lease_notification_for_delivery(notification_id, now=now)
        if row is None:
            return False

        decoded = self._decode_notification_row(row)
        session_key: str | None = None
        try:
            session_key = await openclaw_service.dispatch_notification(decoded)
        except Exception as exc:
            await openclaw_service.mark_delivery_failure(
                notification_id,
                int(row.get("delivery_attempt_count") or 1),
                str(exc),
                notification_type=decoded.get("notification_type"),
                timestamp=now.isoformat(),
            )
            return False

        if session_key is None:
            # No delivery route (e.g. API-created project with no channel).
            # Resolve the notification so it won't be retried.
            await self._resolve_pending_notifications(
                NotificationEntityType(decoded.get("entity_type", "project")),
                decoded.get("entity_id", ""),
                now=now,
            )
            return True

        await openclaw_service.mark_delivery_success(notification_id, timestamp=now.isoformat())

        return True

    async def _lease_notification_for_delivery(self, notification_id: str, *, now: datetime) -> dict[str, Any] | None:
        updated = await self.db.execute(
            """
            UPDATE notifications
            SET delivery_status = ?, delivery_attempt_count = COALESCE(delivery_attempt_count, 0) + 1, updated_at = ?
            WHERE id = ? AND status = ? AND delivery_status IN (?, ?)
              AND (next_delivery_at IS NULL OR next_delivery_at <= ?)
            """,
            (
                NotificationDeliveryStatus.SENDING.value,
                now.isoformat(),
                notification_id,
                NotificationStatus.PENDING.value,
                NotificationDeliveryStatus.PENDING.value,
                NotificationDeliveryStatus.FAILED.value,
                now.isoformat(),
            ),
        )
        if updated == 0:
            return None
        return await self._get_notification_row(notification_id)

    # ── Internal: helpers ───────────────────────────────────────

    async def _get_parent_project(self, task_id: str) -> dict[str, Any] | None:
        return await _get_parent_project(self.db, task_id)

    async def _is_project_muted(self, project_id: str) -> bool:
        row = await self.db.fetch_one(
            "SELECT notifications_muted FROM projects WHERE id = ? AND deleted_at IS NULL",
            (project_id,),
        )
        return bool(row["notifications_muted"]) if row else False

    async def _get_notification_row(self, notification_id: str) -> dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM notifications WHERE id = ?", (notification_id,))
        if row is None:
            raise NotFoundError(f"Notification '{notification_id}' was not found")
        return row

    def _decode_notification_row(self, row: dict[str, Any]) -> dict[str, Any]:
        decoded = dict(row)
        decoded["metadata"] = json_loads(decoded.get("metadata"), {})
        decoded["entity_id"] = UUID(decoded["entity_id"])
        return decoded
