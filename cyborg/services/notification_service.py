"""Business rules for persisted client notifications.

Simplified: only project-level notifications for COMPLETED and BLOCKED states.
Task assignments are dispatched as reasoning prompts at task creation time.
No periodic scanning or repeat logic - all notifications fire once.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from aiosqlite import Connection

from cyborg.database import Database
from cyborg.exceptions import NotFoundError
from cyborg.models import (
    NotificationAcknowledgeRequest,
    NotificationDeliveryStatus,
    NotificationEntityType,
    NotificationResponse,
    NotificationStatus,
    NotificationType,
    ProjectState,
)
from cyborg.services.base import BaseService, json_dumps, json_loads, utcnow
from cyborg.services.openclaw_hook_service import OpenClawHookService


class NotificationService(BaseService):
    """Create, acknowledge, resolve, and dispatch persisted notifications.

    Only two notification scenarios remain:
    - PROJECT_RESULT: when a project is completed (auto or manual close)
    - NEEDS_INPUT: when a project is blocked (PAUSED + blocked_reason)

    Both fire exactly once. No repeat logic or periodic scanning.
    """

    def __init__(self, db: Database, openclaw_service: OpenClawHookService | None = None) -> None:
        super().__init__(db)
        self._openclaw_service = openclaw_service

    def _get_openclaw_service(self) -> OpenClawHookService:
        if self._openclaw_service is None:
            from cyborg.config import Settings
            settings = getattr(self.db, "settings", None)
            public_url = ""
            if isinstance(settings, Settings):
                public_url = settings.resolved_public_url
            self._openclaw_service = OpenClawHookService(self.db, cyborg_service_url=public_url)
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

        # Fire-once: skip if a pending NEEDS_INPUT already exists for this project
        existing = await self.db.fetch_one(
            """
            SELECT id FROM notifications
            WHERE entity_type = ? AND entity_id = ? AND notification_type = ? AND status = ?
            LIMIT 1
            """,
            (
                NotificationEntityType.PROJECT.value,
                project_id,
                NotificationType.NEEDS_INPUT.value,
                NotificationStatus.PENDING.value,
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

    # ── Task assignment (reasoning prompt dispatch) ─────────────

    async def create_task_assignment_notification(
        self,
        task_id: str,
        *,
        now: datetime | None = None,
    ) -> None:
        """Create a TASK_ASSIGNMENT notification for a newly created task.

        This dispatches as a reasoning prompt (agent RPC) to OpenClaw.
        Called directly from task_service at task creation time.
        """
        row = await self.db.fetch_one(
            "SELECT * FROM tasks WHERE id = ? AND deleted_at IS NULL",
            (task_id,),
        )
        if row is None:
            return

        task_metadata = json_loads(row.get("metadata"), {})
        target_session = task_metadata.get("target_session")
        has_source_route = any(task_metadata.get(field) for field in ("session_key", "chat_id", "channel"))
        delivery_route = "target" if isinstance(target_session, dict) else ("source" if has_source_route else None)

        if delivery_route is None:
            return

        source_updated_at = row.get("updated_at")
        parent_project = await self._get_parent_project(task_id)

        title = f"Task to action: {row['title']}"
        message_parts = []
        if row.get("description"):
            message_parts.append(row["description"])
        if row.get("plan"):
            message_parts.append(f"Plan:\n{row['plan']}")
        if row.get("requested_by"):
            message_parts.append(f"Requested by: {row['requested_by']}")
        if parent_project is not None:
            message_parts.append(f"Project: {parent_project['title']} ({parent_project['id']})")
        message = "\n\n".join(part for part in message_parts if part) or "A task is ready to action."

        # Compute output directory for the task
        output_directory = None
        if parent_project is not None:
            try:
                from cyborg.services.project_service import ProjectService

                project_service = ProjectService(self.db)
                project_path = await project_service.get_project_path(parent_project["id"])
                short_id = row["id"].replace("-", "")[:8]
                output_directory = str(project_path / "tasks" / short_id)
            except Exception:
                pass

        metadata = {
            **task_metadata,
            "task_id": row["id"],
            "task_status": row["status"],
            "parent_project_id": parent_project["id"] if parent_project else None,
            "parent_project_title": parent_project["title"] if parent_project else None,
            "delivery_route": delivery_route,
            "output_directory": output_directory,
        }
        await self._create_entity_notification(
            entity_type=NotificationEntityType.TASK,
            entity_id=row["id"],
            notification_type=NotificationType.TASK_ASSIGNMENT,
            title=title,
            message=message,
            metadata=metadata,
            now=now or utcnow(),
            source_updated_at=source_updated_at,
        )

    # ── Task retry (submission rejected) ──────────────────────────

    async def create_task_retry_notification(
        self,
        task_id: str,
        review_feedback: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> None:
        """Create a TASK_RETRY notification when a submission is rejected by review.

        Dispatches to the same session as the original task assignment so
        the agent can retry in context.
        """
        row = await self.db.fetch_one(
            "SELECT * FROM tasks WHERE id = ? AND deleted_at IS NULL",
            (task_id,),
        )
        if row is None:
            return

        task_metadata = json_loads(row.get("metadata"), {})
        target_session = task_metadata.get("target_session")
        has_source_route = any(task_metadata.get(field) for field in ("session_key", "chat_id", "channel"))
        delivery_route = "target" if isinstance(target_session, dict) else ("source" if has_source_route else None)

        if delivery_route is None:
            return

        source_updated_at = row.get("updated_at")
        parent_project = await self._get_parent_project(task_id)

        issues = review_feedback.get("issues", [])
        suggestions = review_feedback.get("suggestions", [])
        reasoning = review_feedback.get("reasoning", "")

        message_parts = [
            f"Task submission rejected: {row['title']}",
            "",
            f"Reason: {reasoning}",
        ]
        if issues:
            message_parts.append("")
            message_parts.append("Issues:")
            for issue in issues:
                message_parts.append(f"  - {issue}")
        if suggestions:
            message_parts.append("")
            message_parts.append("Suggestions:")
            for suggestion in suggestions:
                message_parts.append(f"  - {suggestion}")

        message = "\n".join(message_parts)

        output_directory = None
        if parent_project is not None:
            try:
                from cyborg.services.project_service import ProjectService

                project_service = ProjectService(self.db)
                project_path = await project_service.get_project_path(parent_project["id"])
                short_id = row["id"].replace("-", "")[:8]
                output_directory = str(project_path / "tasks" / short_id)
            except Exception:
                pass

        metadata = {
            **task_metadata,
            "task_id": row["id"],
            "task_status": "active",
            "parent_project_id": parent_project["id"] if parent_project else None,
            "parent_project_title": parent_project["title"] if parent_project else None,
            "delivery_route": delivery_route,
            "output_directory": output_directory,
            "review_feedback": review_feedback,
        }

        await self._create_entity_notification(
            entity_type=NotificationEntityType.TASK,
            entity_id=row["id"],
            notification_type=NotificationType.TASK_RETRY,
            title=f"Task retry: {row['title']}",
            message=message,
            metadata=metadata,
            now=now or utcnow(),
            source_updated_at=source_updated_at,
        )

    # ── Task input response (user answered a task input request) ───

    async def create_task_input_response_notification(
        self,
        task_id: str,
        input_response: str | list[str],
        input_prompt: str,
        approval_id: str,
        *,
        now: datetime | None = None,
    ) -> None:
        """Create a TASK_INPUT_RESPONSE notification when the user answers a task input request.

        Dispatches to the same session as the original task assignment so
        the agent can resume work with the user's input.
        """
        row = await self.db.fetch_one(
            "SELECT * FROM tasks WHERE id = ? AND deleted_at IS NULL",
            (task_id,),
        )
        if row is None:
            return

        task_metadata = json_loads(row.get("metadata"), {})
        target_session = task_metadata.get("target_session")
        has_source_route = any(task_metadata.get(field) for field in ("session_key", "chat_id", "channel"))
        delivery_route = "target" if isinstance(target_session, dict) else ("source" if has_source_route else None)

        if delivery_route is None:
            return

        source_updated_at = row.get("updated_at")
        parent_project = await self._get_parent_project(task_id)

        if isinstance(input_response, list):
            response_text = ", ".join(input_response)
        else:
            response_text = input_response

        message = f"Question: {input_prompt}\n\nUser response: {response_text}"

        output_directory = None
        if parent_project is not None:
            try:
                from cyborg.services.project_service import ProjectService

                project_service = ProjectService(self.db)
                project_path = await project_service.get_project_path(parent_project["id"])
                short_id = row["id"].replace("-", "")[:8]
                output_directory = str(project_path / "tasks" / short_id)
            except Exception:
                pass

        metadata = {
            **task_metadata,
            "task_id": row["id"],
            "task_status": "active",
            "parent_project_id": parent_project["id"] if parent_project else None,
            "parent_project_title": parent_project["title"] if parent_project else None,
            "delivery_route": delivery_route,
            "output_directory": output_directory,
            "input_response": input_response,
            "input_prompt": input_prompt,
            "approval_id": approval_id,
        }

        await self._create_entity_notification(
            entity_type=NotificationEntityType.TASK,
            entity_id=row["id"],
            notification_type=NotificationType.TASK_INPUT_RESPONSE,
            title=f"Task input received: {row['title']}",
            message=message,
            metadata=metadata,
            now=now or utcnow(),
            source_updated_at=source_updated_at,
        )

    # ── Task tap (operator nudge) ───────────────────────────────────

    async def create_task_tap_notification(
        self,
        task_id: str,
        *,
        now: datetime | None = None,
    ) -> str | None:
        """Create a TASK_TAP notification to nudge an agent on an active task.

        Returns the notification ID on success, or None if the task has no
        delivery route.
        """
        row = await self.db.fetch_one(
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

        source_updated_at = row.get("updated_at")
        parent_project = await self._get_parent_project(task_id)

        output_directory = None
        if parent_project is not None:
            try:
                from cyborg.services.project_service import ProjectService

                project_service = ProjectService(self.db)
                project_path = await project_service.get_project_path(parent_project["id"])
                short_id = row["id"].replace("-", "")[:8]
                output_directory = str(project_path / "tasks" / short_id)
            except Exception:
                pass

        metadata = {
            **task_metadata,
            "task_id": row["id"],
            "task_status": row["status"],
            "parent_project_id": parent_project["id"] if parent_project else None,
            "parent_project_title": parent_project["title"] if parent_project else None,
            "delivery_route": delivery_route,
            "output_directory": output_directory,
        }

        notification_id = str(uuid4())
        reference = now or utcnow()
        now_iso = reference.isoformat()

        async with self.db.connection(write=True) as connection:
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
                    NotificationEntityType.TASK.value,
                    row["id"],
                    NotificationType.TASK_TAP.value,
                    NotificationStatus.PENDING.value,
                    NotificationDeliveryStatus.PENDING.value,
                    now_iso,
                    f"Task tap: {row['title']}",
                    f"Status check on active task: {row['title']}",
                    json_dumps(metadata),
                    None,
                    now_iso,
                    now_iso,
                    source_updated_at,
                ),
            )

        await self._attempt_dispatch_notification(notification_id, now=reference)
        return notification_id

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

    # ── Internal: dispatch ──────────────────────────────────────

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
        try:
            await openclaw_service.dispatch_notification(decoded)
        except Exception as exc:
            await openclaw_service.mark_delivery_failure(
                notification_id,
                int(row.get("delivery_attempt_count") or 1),
                str(exc),
                notification_type=decoded.get("notification_type"),
                timestamp=now.isoformat(),
            )
            return False

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
        return await self.db.fetch_one(
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
