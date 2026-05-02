"""Track agent dispatches from notification delivery to task completion."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any
from uuid import uuid4

from cyborg_server.database import Database
from cyborg_server.models import DispatchResponse, DispatchStatus
from cyborg_server.services.base import BaseService, json_loads, utcnow

logger = logging.getLogger(__name__)


class DispatchService(BaseService):
    """Record, query, and manage the lifecycle of agent dispatches to OpenClaw."""

    async def record_dispatch(
        self,
        *,
        notification_id: str | None = None,
        notification_type: str,
        session_key: str,
        task_id: str | None = None,
        project_id: str | None = None,
    ) -> str:
        """Record a new dispatch, cancelling any prior active dispatch for the same notification."""
        now = utcnow()
        now_iso = now.isoformat()
        dispatch_id = str(uuid4())

        async with self.db.connection(write=True) as conn:
            if notification_id:
                await conn.execute(
                    "UPDATE dispatches SET status = ?, updated_at = ? WHERE notification_id = ? AND status = ?",
                    (DispatchStatus.CANCELLED.value, now_iso, notification_id, DispatchStatus.ACTIVE.value),
                )
            await conn.execute(
                """
                INSERT INTO dispatches
                    (id, notification_id, notification_type, session_key, task_id, project_id,
                     status, dispatched_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dispatch_id,
                    notification_id,
                    notification_type,
                    session_key,
                    task_id,
                    project_id,
                    DispatchStatus.ACTIVE.value,
                    now_iso,
                    now_iso,
                    now_iso,
                ),
            )

        logger.info(
            "Recorded dispatch %s for %s (session=%s, task=%s, project=%s)",
            dispatch_id, notification_type, session_key, task_id, project_id,
        )
        return dispatch_id

    def track(self, dispatch_id: str, coro: Coroutine[Any, Any, Any]) -> None:
        """Run *coro* in the background, updating dispatch status on completion.

        The caller remains non-blocking.  The dispatch is marked completed when
        the gateway returns, failed on error, or timed_out on asyncio.TimeoutError.
        If the agent never finishes the dispatch stays active (stuck) and can be tapped.
        """
        db = self.db

        async def _run() -> None:
            try:
                await coro
                await DispatchService(db)._complete(dispatch_id)
            except asyncio.TimeoutError:
                logger.warning("Dispatch %s timed out waiting for gateway final response", dispatch_id)
                await DispatchService(db)._update_status(dispatch_id, DispatchStatus.TIMED_OUT)
            except Exception:
                logger.exception("Dispatch %s failed during gateway call", dispatch_id)
                await DispatchService(db)._update_status(dispatch_id, DispatchStatus.FAILED)

        asyncio.create_task(_run())

    async def _complete(self, dispatch_id: str) -> None:
        """Mark a single dispatch as completed with duration."""
        now = utcnow()
        now_iso = now.isoformat()
        await self.db.execute(
            """
            UPDATE dispatches
            SET status = ?, completed_at = ?, updated_at = ?,
                duration_seconds = (julianday(?) - julianday(dispatched_at)) * 86400
            WHERE id = ? AND status = ?
            """,
            (DispatchStatus.COMPLETED.value, now_iso, now_iso, now_iso, dispatch_id, DispatchStatus.ACTIVE.value),
        )
        logger.info("Dispatch %s completed", dispatch_id)

    async def _update_status(self, dispatch_id: str, status: DispatchStatus) -> None:
        now = utcnow()
        now_iso = now.isoformat()
        await self.db.execute(
            """
            UPDATE dispatches
            SET status = ?, completed_at = ?, updated_at = ?,
                duration_seconds = (julianday(?) - julianday(dispatched_at)) * 86400
            WHERE id = ? AND status = ?
            """,
            (status.value, now_iso, now_iso, now_iso, dispatch_id, DispatchStatus.ACTIVE.value),
        )

    async def complete_dispatches_for_task(
        self,
        task_id: str,
        *,
        status: DispatchStatus = DispatchStatus.COMPLETED,
    ) -> int:
        """Mark all active dispatches for a task as completed or failed."""
        now = utcnow()
        now_iso = now.isoformat()
        cursor = await self.db.execute(
            """
            UPDATE dispatches
            SET status = ?, completed_at = ?, updated_at = ?,
                duration_seconds = (julianday(?) - julianday(dispatched_at)) * 86400
            WHERE task_id = ? AND status = ?
            """,
            (status.value, now_iso, now_iso, now_iso, task_id, DispatchStatus.ACTIVE.value),
        )
        count = cursor.rowcount if hasattr(cursor, "rowcount") else 0
        if count:
            logger.info("Completed %d dispatch(es) for task %s as %s", count, task_id, status.value)
        return count

    async def complete_dispatches_for_project(
        self,
        project_id: str,
        *,
        status: DispatchStatus = DispatchStatus.COMPLETED,
    ) -> int:
        """Mark all active dispatches for a project as completed or failed."""
        now = utcnow()
        now_iso = now.isoformat()
        cursor = await self.db.execute(
            """
            UPDATE dispatches
            SET status = ?, completed_at = ?, updated_at = ?,
                duration_seconds = (julianday(?) - julianday(dispatched_at)) * 86400
            WHERE project_id = ? AND status = ?
            """,
            (status.value, now_iso, now_iso, now_iso, project_id, DispatchStatus.ACTIVE.value),
        )
        count = cursor.rowcount if hasattr(cursor, "rowcount") else 0
        if count:
            logger.info("Completed %d dispatch(es) for project %s as %s", count, project_id, status.value)
        return count

    async def list_active_dispatches(self, *, limit: int = 100) -> list[DispatchResponse]:
        """Return active dispatches with task/project titles."""
        rows = await self.db.fetch_all(
            """
            SELECT d.*,
                   t.title AS task_title,
                   p.title AS project_title
            FROM dispatches d
            LEFT JOIN tasks t ON t.id = d.task_id AND t.deleted_at IS NULL
            LEFT JOIN projects p ON p.id = d.project_id AND p.deleted_at IS NULL
            WHERE d.status = ?
            ORDER BY d.dispatched_at ASC
            LIMIT ?
            """,
            (DispatchStatus.ACTIVE.value, limit),
        )
        return [self._row_to_response(row) for row in rows]

    async def get_stuck_dispatches(self, *, timeout_minutes: float = 60.0) -> list[DispatchResponse]:
        """Return active dispatches older than the timeout threshold."""
        rows = await self.db.fetch_all(
            """
            SELECT d.*,
                   t.title AS task_title,
                   p.title AS project_title
            FROM dispatches d
            LEFT JOIN tasks t ON t.id = d.task_id AND t.deleted_at IS NULL
            LEFT JOIN projects p ON p.id = d.project_id AND p.deleted_at IS NULL
            WHERE d.status = ?
              AND (julianday(?) - julianday(d.dispatched_at)) * 1440 > ?
            ORDER BY d.dispatched_at ASC
            """,
            (DispatchStatus.ACTIVE.value, utcnow().isoformat(), timeout_minutes),
        )
        return [self._row_to_response(row) for row in rows]

    async def get_dispatch(self, dispatch_id: str) -> DispatchResponse | None:
        """Return a single dispatch by ID."""
        row = await self.db.fetch_one(
            """
            SELECT d.*,
                   t.title AS task_title,
                   p.title AS project_title
            FROM dispatches d
            LEFT JOIN tasks t ON t.id = d.task_id AND t.deleted_at IS NULL
            LEFT JOIN projects p ON p.id = d.project_id AND p.deleted_at IS NULL
            WHERE d.id = ?
            """,
            (dispatch_id,),
        )
        if row is None:
            return None
        return self._row_to_response(row)

    async def tap_dispatch(self, dispatch_id: str) -> DispatchResponse | None:
        """Tap a stuck dispatch: increment tap_count and send a task_tap notification."""
        now = utcnow()
        now_iso = now.isoformat()

        dispatch = await self.get_dispatch(dispatch_id)
        if dispatch is None or dispatch.status != DispatchStatus.ACTIVE:
            return None

        if not dispatch.task_id:
            logger.warning("Cannot tap dispatch %s — no task_id", dispatch_id)
            return None

        # Increment tap count
        await self.db.execute(
            "UPDATE dispatches SET tap_count = tap_count + 1, last_tapped_at = ?, updated_at = ? WHERE id = ?",
            (now_iso, now_iso, dispatch_id),
        )

        # Create a task tap notification
        from cyborg_server.services.notification_service import NotificationService
        notification_service = NotificationService(self.db)
        await notification_service.create_task_tap_notification(str(dispatch.task_id), now=now)

        logger.info("Tapped dispatch %s (tap_count now %d)", dispatch_id, dispatch.tap_count + 1)
        return await self.get_dispatch(dispatch_id)

    async def cancel_dispatch(self, dispatch_id: str) -> DispatchResponse | None:
        """Cancel an active dispatch."""
        now = utcnow()
        now_iso = now.isoformat()
        updated = await self.db.execute(
            "UPDATE dispatches SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
            (DispatchStatus.CANCELLED.value, now_iso, dispatch_id, DispatchStatus.ACTIVE.value),
        )
        if not updated:
            return None
        return await self.get_dispatch(dispatch_id)

    async def cancel_active_dispatches(self) -> int:
        """Cancel all active dispatches. Used during shutdown timeout."""
        now = utcnow()
        cursor = await self.db.execute(
            "UPDATE dispatches SET status = ?, updated_at = ? WHERE status = ?",
            (DispatchStatus.CANCELLED.value, now.isoformat(), DispatchStatus.ACTIVE.value),
        )
        count = cursor.rowcount if hasattr(cursor, "rowcount") else 0
        if count:
            logger.warning("Cancelled %d active dispatch(es) during shutdown", count)
        return count

    async def count_active_dispatches(self) -> int:
        """Return the number of currently active dispatches."""
        row = await self.db.fetch_one(
            "SELECT COUNT(*) AS cnt FROM dispatches WHERE status = ?",
            (DispatchStatus.ACTIVE.value,),
        )
        return row["cnt"] if row else 0

    async def wait_for_active_dispatches(
        self,
        *,
        timeout_seconds: float = 30.0,
        poll_interval: float = 2.0,
    ) -> None:
        """Poll until no active dispatches remain or timeout expires."""
        elapsed = 0.0
        while elapsed < timeout_seconds:
            count = await self.count_active_dispatches()
            if count == 0:
                return
            logger.info("Waiting for %d active dispatch(es) to complete (%.0fs remaining)", count, timeout_seconds - elapsed)
            await asyncio.sleep(min(poll_interval, timeout_seconds - elapsed))
            elapsed += poll_interval

        # Timeout expired — cancel remaining
        remaining = await self.count_active_dispatches()
        if remaining:
            logger.warning("Shutdown timeout expired with %d dispatch(es) still active — cancelling", remaining)
            await self.cancel_active_dispatches()

    def _row_to_response(self, row: dict[str, Any]) -> DispatchResponse:
        from uuid import UUID

        data = dict(row)
        data["metadata"] = json_loads(data.get("metadata"), {})
        for field in ("task_id", "project_id", "notification_id"):
            if data.get(field) and isinstance(data[field], str):
                data[field] = UUID(data[field])
        return DispatchResponse.model_validate(data)
