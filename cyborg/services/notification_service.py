"""Business rules for persisted client notifications."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from aiosqlite import Connection

from cyborg.database import Database
from cyborg.exceptions import NotFoundError
from cyborg.models import (
    EventStatus,
    NotificationAcknowledgeRequest,
    NotificationDeliveryStatus,
    NotificationEntityType,
    NotificationResponse,
    NotificationStatus,
    NotificationType,
    PlanStatus,
    ProjectSpecStatus,
    ProjectState,
    TaskStatus,
)
from cyborg.services.base import BaseService, json_dumps, json_loads, utcnow
from cyborg.services.openclaw_hook_service import OpenClawHookService


class NotificationService(BaseService):
    """Raise, repeat, resolve, acknowledge, and dispatch persisted notifications."""

    INPUT_REPEAT_INTERVAL = timedelta(days=1)
    INPUT_MAX_RAISES = 4
    DEFAULT_EVENT_REMINDER_MINUTES = 60

    def __init__(self, db: Database, openclaw_service: OpenClawHookService | None = None) -> None:
        super().__init__(db)
        self._openclaw_service = openclaw_service

    def _get_openclaw_service(self) -> OpenClawHookService:
        if self._openclaw_service is None:
            # Get the public URL from settings if available
            from cyborg.config import Settings
            settings = getattr(self.db, "settings", None)
            public_url = ""
            if isinstance(settings, Settings):
                public_url = settings.resolved_public_url
            self._openclaw_service = OpenClawHookService(self.db, cyborg_service_url=public_url)
        return self._openclaw_service

    async def list_notifications(
        self,
        *,
        status: NotificationStatus | None = NotificationStatus.PENDING,
        entity_type: NotificationEntityType | None = None,
        limit: int = 100,
    ) -> list[NotificationResponse]:
        await self.sync_due_notifications()

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

    async def process_due_notifications(self, *, now: datetime | None = None) -> int:
        reference = now or utcnow()
        await self.sync_due_notifications(now=reference)
        return await self._dispatch_due_notifications(reference)

    async def sync_due_notifications(self, *, now: datetime | None = None) -> None:
        reference = now or utcnow()

        task_rows = await self.db.fetch_all(
            """
            SELECT *
            FROM tasks
            WHERE deleted_at IS NULL AND status IN (?, ?, ?, ?)
            ORDER BY created_at ASC
            """,
            (
                TaskStatus.PLANNING.value,
                TaskStatus.BLOCKED.value,
                TaskStatus.PENDING.value,
                TaskStatus.ACTIVE.value,
            ),
        )
        for row in task_rows:
            await self._sync_task_row(row, reference, immediate=False)

        project_rows = await self.db.fetch_all(
            """
            SELECT *
            FROM projects
            WHERE deleted_at IS NULL AND state IN (?, ?)
            ORDER BY created_at ASC
            """,
            (ProjectState.PLANNING.value, ProjectState.PAUSED.value),
        )
        for row in project_rows:
            await self._sync_project_row(await self._enrich_project_row(row), reference, immediate=False)

        await self._sync_event_notifications(reference)

    async def sync_task_state(self, task_id: str, *, immediate: bool = False, now: datetime | None = None) -> None:
        row = await self.db.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            await self._resolve_pending_notifications(NotificationEntityType.TASK, task_id, now=now or utcnow())
            return
        await self._sync_task_row(row, now or utcnow(), immediate=immediate)

    async def sync_project_state(
        self,
        project_id: str,
        *,
        immediate: bool = False,
        now: datetime | None = None,
    ) -> None:
        row = await self.db.fetch_one("SELECT * FROM projects WHERE id = ?", (project_id,))
        if row is None:
            await self._resolve_pending_notifications(NotificationEntityType.PROJECT, project_id, now=now or utcnow())
            return
        await self._sync_project_row(await self._enrich_project_row(row), now or utcnow(), immediate=immediate)

    async def create_task_result_notification(
        self,
        task_id: str,
        *,
        failed: bool,
        result_summary: str | None,
        now: datetime | None = None,
    ) -> None:
        row = await self.db.fetch_one("SELECT * FROM tasks WHERE id = ? AND deleted_at IS NULL", (task_id,))
        if row is None:
            return
        task_metadata = json_loads(row.get("metadata"), {})
        title_prefix = "Task failed" if failed else "Task completed"
        title = f"{title_prefix}: {row['title']}"
        if result_summary:
            message = result_summary
        elif failed:
            message = "The task failed without a result summary."
        else:
            message = "The task completed."
        metadata = {
            **task_metadata,
            "task_id": row["id"],
            "task_status": row["status"],
            "delivery_route": "source",
        }
        await self._create_entity_notification(
            entity_type=NotificationEntityType.TASK,
            entity_id=row["id"],
            notification_type=NotificationType.TASK_RESULT,
            title=title,
            message=message,
            metadata=metadata,
            now=now or utcnow(),
            source_updated_at=row.get("completed_at") or row.get("updated_at"),
        )

    async def create_project_result_notification(
        self,
        project_id: str,
        *,
        conclusion: str | None = None,
        now: datetime | None = None,
    ) -> None:
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

    async def _sync_task_row(self, row: dict[str, Any], now: datetime, *, immediate: bool) -> None:
        task_id = row["id"]
        await self._sync_task_assignment_notification(row, now=now)

        if row.get("deleted_at") is not None or not await self._task_needs_input(row):
            await self._clear_needs_input_since(NotificationEntityType.TASK, task_id)
            await self._resolve_pending_notifications(
                NotificationEntityType.TASK,
                task_id,
                now=now,
                notification_types={NotificationType.NEEDS_INPUT},
            )
            return

        if immediate:
            await self._set_needs_input_since(NotificationEntityType.TASK, task_id, now)
            await self._resolve_pending_notifications(
                NotificationEntityType.TASK,
                task_id,
                now=now,
                notification_types={NotificationType.NEEDS_INPUT},
            )
            await self._create_task_input_notification(row, now)
            return

        needs_input_since = row.get("needs_input_since")
        if not needs_input_since:
            await self._set_needs_input_since(NotificationEntityType.TASK, task_id, now)
            await self._create_task_input_notification(row, now)
            return

        raises = await self._count_input_notifications_since(NotificationEntityType.TASK, task_id, needs_input_since)
        last_notification_at = self._parse_datetime(row.get("last_notification_at"))
        if raises == 0 or last_notification_at is None:
            await self._create_task_input_notification(row, now)
            return

        if raises < self.INPUT_MAX_RAISES and now - last_notification_at >= self.INPUT_REPEAT_INTERVAL:
            await self._create_task_input_notification(row, now)

    async def _sync_task_assignment_notification(self, row: dict[str, Any], *, now: datetime) -> None:
        task_id = row["id"]
        task_metadata = json_loads(row.get("metadata"), {})
        target_session = task_metadata.get("target_session")
        has_source_route = any(task_metadata.get(field) for field in ("session_key", "chat_id", "channel"))
        delivery_route = "target" if isinstance(target_session, dict) else ("source" if has_source_route else None)
        if (
            row.get("deleted_at") is not None
            or row["status"] not in {TaskStatus.PENDING.value, TaskStatus.ACTIVE.value}
            or delivery_route is None
        ):
            await self._resolve_pending_notifications(
                NotificationEntityType.TASK,
                task_id,
                now=now,
                notification_types={NotificationType.TASK_ASSIGNMENT},
            )
            return

        source_updated_at = row.get("current_plan_id") or row.get("updated_at")
        existing = await self.db.fetch_one(
            """
            SELECT id
            FROM notifications
            WHERE entity_type = ? AND entity_id = ? AND notification_type = ? AND source_updated_at = ? AND status = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (
                NotificationEntityType.TASK.value,
                task_id,
                NotificationType.TASK_ASSIGNMENT.value,
                source_updated_at,
                NotificationStatus.PENDING.value,
            ),
        )
        if existing is None:
            await self._create_task_assignment_notification(
                row,
                now,
                source_updated_at=source_updated_at,
                delivery_route=delivery_route,
            )

    async def _sync_project_row(self, row: dict[str, Any], now: datetime, *, immediate: bool) -> None:
        project_id = row["id"]
        if row.get("deleted_at") is not None or not self._project_needs_input(row):
            await self._clear_needs_input_since(NotificationEntityType.PROJECT, project_id)
            await self._resolve_pending_notifications(
                NotificationEntityType.PROJECT,
                project_id,
                now=now,
                notification_types={NotificationType.NEEDS_INPUT},
            )
            return

        if immediate:
            await self._set_needs_input_since(NotificationEntityType.PROJECT, project_id, now)
            await self._resolve_pending_notifications(
                NotificationEntityType.PROJECT,
                project_id,
                now=now,
                notification_types={NotificationType.NEEDS_INPUT},
            )
            await self._create_project_input_notification(row, now)
            return

        needs_input_since = row.get("needs_input_since")
        if not needs_input_since:
            await self._set_needs_input_since(NotificationEntityType.PROJECT, project_id, now)
            await self._create_project_input_notification(row, now)
            return

        raises = await self._count_input_notifications_since(
            NotificationEntityType.PROJECT,
            project_id,
            needs_input_since,
        )
        last_notification_at = self._parse_datetime(row.get("last_notification_at"))
        if raises == 0 or last_notification_at is None:
            await self._create_project_input_notification(row, now)
            return

        if raises < self.INPUT_MAX_RAISES and now - last_notification_at >= self.INPUT_REPEAT_INTERVAL:
            await self._create_project_input_notification(row, now)

    async def _sync_event_notifications(self, now: datetime) -> None:
        pending_rows = await self.db.fetch_all(
            """
            SELECT *
            FROM notifications
            WHERE entity_type = ? AND notification_type = ? AND status = ?
            """,
            (
                NotificationEntityType.EVENT.value,
                NotificationType.EVENT_REMINDER.value,
                NotificationStatus.PENDING.value,
            ),
        )
        for notification in pending_rows:
            event = await self.db.fetch_one(
                """
                SELECT e.*, c.name AS calendar_name, c.metadata AS calendar_metadata
                FROM events AS e
                INNER JOIN calendars AS c ON c.id = e.calendar_id
                WHERE e.id = ? AND e.deleted_at IS NULL AND c.deleted_at IS NULL
                """,
                (notification["entity_id"],),
            )
            if event is None or not self._event_notification_is_current(event, notification, now):
                await self._resolve_notification_row(notification["id"], now)

        due_events = await self.db.fetch_all(
            """
            SELECT e.*, c.name AS calendar_name, c.metadata AS calendar_metadata
            FROM events AS e
            INNER JOIN calendars AS c ON c.id = e.calendar_id
            WHERE e.deleted_at IS NULL AND c.deleted_at IS NULL AND e.status != ?
            ORDER BY e.start_time ASC
            """,
            (EventStatus.CANCELLED.value,),
        )
        for row in due_events:
            if not self._event_reminder_is_due(row, now):
                continue
            existing = await self.db.fetch_one(
                """
                SELECT id
                FROM notifications
                WHERE entity_type = ? AND entity_id = ? AND notification_type = ? AND source_updated_at = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (
                    NotificationEntityType.EVENT.value,
                    row["id"],
                    NotificationType.EVENT_REMINDER.value,
                    row["updated_at"],
                ),
            )
            if existing is None:
                await self._create_event_reminder_notification(row, now)

    async def _create_task_input_notification(self, row: dict[str, Any], now: datetime) -> None:
        latest_plan = await self.db.fetch_one(
            """
            SELECT status, feedback
            FROM plans
            WHERE task_id = ?
            ORDER BY version_number DESC
            LIMIT 1
            """,
            (row["id"],),
        )
        parent_project = await self._get_parent_project(row["id"])
        task_metadata = json_loads(row.get("metadata"), {})

        if row["status"] == TaskStatus.BLOCKED.value:
            title = f"Task needs input: {row['title']}"
            message = row.get("blocked_reason") or "This task is blocked and waiting for user input."
            if row.get("blocked_resume_instructions"):
                message += f"\n\nResume instructions: {row['blocked_resume_instructions']}"
        elif latest_plan and latest_plan["status"] == PlanStatus.REJECTED.value:
            title = f"Task plan needs revision: {row['title']}"
            message = "The latest task plan was rejected."
            if latest_plan.get("feedback"):
                message += f"\n\nFeedback: {latest_plan['feedback']}"
        elif latest_plan and latest_plan["status"] == PlanStatus.PENDING_APPROVAL.value:
            title = f"Task plan awaiting approval: {row['title']}"
            message = "The task is waiting for plan approval before it can move to pending."
            # Include the plan content for approval
            if row.get("plan"):
                message += f"\n\nProposed plan:\n{row['plan']}"
        else:
            title = f"Task needs planning: {row['title']}"
            message = "The task still needs a usable approved plan before it can start."

        if parent_project is not None:
            message += f"\n\nProject: {parent_project['title']} ({parent_project['id']})"

        # Include task ID for reference
        message += f"\n\nTask ID: {row['id']}"

        metadata = {
            **task_metadata,
            "task_id": row["id"],
            "task_status": row["status"],
            "blocked_reason": row.get("blocked_reason"),
            "blocked_resume_instructions": row.get("blocked_resume_instructions"),
            "parent_project_id": parent_project["id"] if parent_project else None,
            "parent_project_title": parent_project["title"] if parent_project else None,
            "delivery_route": "source",
        }
        await self._create_entity_notification(
            entity_type=NotificationEntityType.TASK,
            entity_id=row["id"],
            notification_type=NotificationType.NEEDS_INPUT,
            title=title,
            message=message,
            metadata=metadata,
            now=now,
            source_updated_at=row.get("updated_at"),
        )

    async def _create_task_assignment_notification(
        self,
        row: dict[str, Any],
        now: datetime,
        *,
        source_updated_at: str | None,
        delivery_route: str,
    ) -> None:
        parent_project = await self._get_parent_project(row["id"])
        task_metadata = json_loads(row.get("metadata"), {})
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

        metadata = {
            **task_metadata,
            "task_id": row["id"],
            "task_status": row["status"],
            "parent_project_id": parent_project["id"] if parent_project else None,
            "parent_project_title": parent_project["title"] if parent_project else None,
            "delivery_route": delivery_route,
        }
        await self._create_entity_notification(
            entity_type=NotificationEntityType.TASK,
            entity_id=row["id"],
            notification_type=NotificationType.TASK_ASSIGNMENT,
            title=title,
            message=message,
            metadata=metadata,
            now=now,
            source_updated_at=source_updated_at,
        )

    async def _create_project_input_notification(self, row: dict[str, Any], now: datetime) -> None:
        project_metadata = json_loads(row.get("metadata"), {})

        if row["state"] == ProjectState.PAUSED.value and row.get("blocked_reason"):
            title = f"Project needs input: {row['title']}"
            message = row["blocked_reason"]
            if row.get("blocked_resume_instructions"):
                message += f"\n\nResume instructions: {row['blocked_resume_instructions']}"
        elif row.get("latest_spec_status") == ProjectSpecStatus.PENDING_APPROVAL.value:
            title = f"Project spec needs approval: {row['title']}"
            latest_aim = row.get("latest_spec_aim") or row.get("aim")
            latest_method = row.get("latest_spec_method") or row.get("method")
            criteria = row.get("latest_spec_success_criteria") or row.get("success_criteria") or []
            lines = []
            if latest_aim:
                lines.append(f"Aim: {latest_aim}")
            if latest_method:
                lines.append(f"Method: {latest_method}")
            if criteria:
                lines.append("Success criteria:")
                for criterion in criteria:
                    description = criterion.get("description") if isinstance(criterion, dict) else None
                    if description:
                        lines.append(f"- {description}")
            message = "\n".join(lines) if lines else "This project spec is waiting for approval."
        elif row.get("latest_spec_status") == ProjectSpecStatus.REJECTED.value and not row.get("current_spec_id"):
            title = f"Project spec needs revision: {row['title']}"
            message = row.get("latest_spec_feedback") or "The latest project spec was rejected and needs revision."
        else:
            title = f"Project needs planning: {row['title']}"
            message = row.get("aim") or row.get("description") or "This project is waiting for planning input."

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
            source_updated_at=row.get("paused_at") or row.get("created_at") or row.get("updated_at"),
        )

    async def _create_event_reminder_notification(self, row: dict[str, Any], now: datetime) -> None:
        calendar_metadata = json_loads(row.get("calendar_metadata"), {})
        start_time = self._parse_datetime(row["start_time"], row.get("timezone"))
        formatted_start = start_time.isoformat() if start_time else row["start_time"]
        title = f"Upcoming event: {row['title']}"
        message = f"Starts at {formatted_start}"
        if row.get("venue"):
            message += f"\n\nVenue: {row['venue']}"
        message += f"\n\nCalendar: {row['calendar_name']}"

        metadata = {
            **calendar_metadata,
            "event_id": row["id"],
            "calendar_id": row["calendar_id"],
            "calendar_name": row["calendar_name"],
            "event_status": row["status"],
            "start_time": row["start_time"],
            "end_time": row["end_time"],
            "venue": row.get("venue"),
            "delivery_route": "source",
        }
        await self._create_entity_notification(
            entity_type=NotificationEntityType.EVENT,
            entity_id=row["id"],
            notification_type=NotificationType.EVENT_REMINDER,
            title=title,
            message=message,
            metadata=metadata,
            now=now,
            source_updated_at=row["updated_at"],
        )

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
        await connection.execute(
            f"UPDATE {table} SET notification_count = ?, last_notification_at = ? WHERE id = ?",
            (next_count, timestamp, entity_id),
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

    async def _resolve_notification_row(self, notification_id: str, now: datetime) -> None:
        await self.db.execute(
            """
            UPDATE notifications
            SET status = ?, resolved_at = ?, updated_at = ?
            WHERE id = ? AND status = ?
            """,
            (
                NotificationStatus.RESOLVED.value,
                now.isoformat(),
                now.isoformat(),
                notification_id,
                NotificationStatus.PENDING.value,
            ),
        )

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

    async def _count_input_notifications_since(
        self,
        entity_type: NotificationEntityType,
        entity_id: str,
        since: str,
    ) -> int:
        row = await self.db.fetch_one(
            """
            SELECT COUNT(*) AS count
            FROM notifications
            WHERE entity_type = ? AND entity_id = ? AND notification_type = ? AND created_at >= ?
            """,
            (
                entity_type.value,
                entity_id,
                NotificationType.NEEDS_INPUT.value,
                since,
            ),
        )
        return int(row["count"]) if row else 0

    async def _set_needs_input_since(
        self,
        entity_type: NotificationEntityType,
        entity_id: str,
        timestamp: datetime,
    ) -> None:
        table = "tasks" if entity_type == NotificationEntityType.TASK else "projects"
        await self.db.execute(
            f"UPDATE {table} SET needs_input_since = ? WHERE id = ?",
            (timestamp.isoformat(), entity_id),
        )

    async def _clear_needs_input_since(self, entity_type: NotificationEntityType, entity_id: str) -> None:
        table = "tasks" if entity_type == NotificationEntityType.TASK else "projects"
        await self.db.execute(
            f"UPDATE {table} SET needs_input_since = NULL WHERE id = ?",
            (entity_id,),
        )

    async def _task_needs_input(self, row: dict[str, Any]) -> bool:
        if row["status"] not in {TaskStatus.PLANNING.value, TaskStatus.BLOCKED.value}:
            return False

        parent_id = row.get("parent_id")
        if parent_id:
            parent = await self.db.fetch_one(
                "SELECT status, deleted_at FROM tasks WHERE id = ?",
                (parent_id,),
            )
            if parent is not None and parent.get("deleted_at") is None and parent["status"] != TaskStatus.COMPLETED.value:
                return False
        return True

    def _project_needs_input(self, row: dict[str, Any]) -> bool:
        if row["state"] == ProjectState.PLANNING.value:
            latest_status = row.get("latest_spec_status")
            if row.get("current_spec_id"):
                return latest_status == ProjectSpecStatus.PENDING_APPROVAL.value
            return True
        return row["state"] == ProjectState.PAUSED.value and bool(row.get("blocked_reason"))

    async def _enrich_project_row(self, row: dict[str, Any]) -> dict[str, Any]:
        latest_spec = await self.db.fetch_one(
            """
            SELECT id, status, aim, method, success_criteria, feedback
            FROM project_specs
            WHERE project_id = ?
            ORDER BY version_number DESC
            LIMIT 1
            """,
            (row["id"],),
        )
        if latest_spec is None:
            row["latest_spec_id"] = None
            row["latest_spec_status"] = None
            row["latest_spec_aim"] = None
            row["latest_spec_method"] = None
            row["latest_spec_success_criteria"] = []
            row["latest_spec_feedback"] = None
            return row

        row["latest_spec_id"] = latest_spec["id"]
        row["latest_spec_status"] = latest_spec["status"]
        row["latest_spec_aim"] = latest_spec["aim"]
        row["latest_spec_method"] = latest_spec["method"]
        row["latest_spec_success_criteria"] = json_loads(latest_spec.get("success_criteria"), [])
        row["latest_spec_feedback"] = latest_spec.get("feedback")
        return row

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

    def _event_reminder_is_due(self, row: dict[str, Any], now: datetime) -> bool:
        if row["status"] == EventStatus.CANCELLED.value:
            return False
        start_time = self._parse_datetime(row["start_time"], row.get("timezone"))
        end_time = self._parse_datetime(row["end_time"], row.get("timezone"))
        if start_time is None or end_time is None or end_time <= now:
            return False
        lead_time = timedelta(minutes=self._event_reminder_minutes(row))
        return start_time - lead_time <= now

    def _event_notification_is_current(
        self,
        event_row: dict[str, Any],
        notification_row: dict[str, Any],
        now: datetime,
    ) -> bool:
        if event_row["status"] == EventStatus.CANCELLED.value:
            return False
        end_time = self._parse_datetime(event_row["end_time"], event_row.get("timezone"))
        if end_time is None or end_time <= now:
            return False
        return event_row["updated_at"] == notification_row.get("source_updated_at")

    def _event_reminder_minutes(self, row: dict[str, Any]) -> int:
        calendar_metadata = json_loads(row.get("calendar_metadata"), {})
        raw_value = calendar_metadata.get("reminder_minutes_before", self.DEFAULT_EVENT_REMINDER_MINUTES)
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            return self.DEFAULT_EVENT_REMINDER_MINUTES
        return max(value, 0)

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

    def _parse_datetime(self, value: str | None, timezone_name: str | None = None) -> datetime | None:
        if not value:
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            return parsed
        if timezone_name:
            try:
                return parsed.replace(tzinfo=ZoneInfo(timezone_name))
            except Exception:
                pass
        return parsed.replace(tzinfo=UTC)
