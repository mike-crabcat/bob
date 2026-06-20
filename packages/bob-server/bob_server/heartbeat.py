"""Registerable background tasks for the heartbeat loop."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

from bob_server.context import AppContext
from bob_server.database import Database


logger = logging.getLogger(__name__)


@runtime_checkable
class HeartbeatTask(Protocol):
    """Protocol for background tasks that run on each heartbeat cycle."""

    name: str

    async def run(self, ctx: AppContext) -> None: ...


class HeartbeatRunner:
    """Runs registered heartbeat tasks on a fixed interval."""

    def __init__(self, ctx: AppContext, *, interval_seconds: float) -> None:
        self._ctx = ctx
        self._interval = interval_seconds
        self._tasks: list[HeartbeatTask] = []

    def register(self, task: HeartbeatTask) -> None:
        self._tasks.append(task)

    async def run_loop(self, stop_event: asyncio.Event) -> None:
        """Run all registered tasks on each cycle until stopped."""
        if self._interval <= 0:
            await stop_event.wait()
            return

        cycle = 0
        while not stop_event.is_set():
            cycle += 1
            for task in self._tasks:
                # Email sync runs every 10 cycles
                if isinstance(task, EmailSyncTask) and cycle % 10 != 0:
                    continue
                try:
                    await task.run(self._ctx)
                except Exception:
                    logger.exception("Heartbeat task %s failed", task.name)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval)
            except TimeoutError:
                continue


class EmailPollingTask:
    """Poll AgentMail inboxes for new email messages."""

    name = "email_polling"

    async def run(self, ctx: AppContext) -> None:
        settings = ctx.settings
        if not settings.agentmail.enabled or not settings.email_polling_enabled:
            return

        from bob_server.services.agentmail_client import AgentMailClient
        from bob_server.services.email_polling_service import EmailPollingService

        client = AgentMailClient(
            base_url=settings.agentmail.base_url,
            api_key=settings.agentmail.api_key,
        )
        try:
            service = EmailPollingService(ctx, agentmail_client=client)
            count = await service.poll_all_inboxes()
            if count > 0:
                logger.info("Email polling processed %d new message(s)", count)
        finally:
            await client.close()


class EmailSyncTask:
    """Periodic full email sync — reconcile AgentMail with local database."""

    name = "email_sync"

    async def run(self, ctx: AppContext) -> None:
        settings = ctx.settings
        if not settings.agentmail.enabled:
            return

        from bob_server.services.agentmail_client import AgentMailClient
        from bob_server.services.email_polling_service import EmailPollingService

        client = AgentMailClient(
            base_url=settings.agentmail.base_url,
            api_key=settings.agentmail.api_key,
        )
        try:
            service = EmailPollingService(ctx, agentmail_client=client)
            count = await service.sync_all_inboxes()
            if count > 0:
                logger.info("Periodic email sync persisted %d missing message(s)", count)
        finally:
            await client.close()


_last_call_cleanup: datetime | None = None


class SessionIdleSummaryTask:
    """Detect idle sessions and generate memory bulletins."""

    name = "session_idle_summary"

    async def _find_idle_sessions(
        self, db: Database, idle_threshold_minutes: float
    ) -> list[dict]:
        rows = await db.fetch_all(
            """
            SELECT
                sm.session_key,
                MAX(sm.created_at) AS last_message_at,
                COALESCE(
                    (SELECT MAX(session_range_end) FROM memory_bulletins
                     WHERE source_id = sm.session_key
                       AND session_range_end != ''),
                    '1970-01-01'
                ) AS active_from,
                COUNT(*) AS message_count
            FROM session_messages sm
            WHERE sm.session_key NOT LIKE 'subagent:%'
              AND datetime(sm.created_at) > datetime(COALESCE(
                (SELECT MAX(session_range_end) FROM memory_bulletins
                 WHERE source_id = sm.session_key
                   AND session_range_end != ''),
                '1970-01-01'
              ))
            GROUP BY sm.session_key
            HAVING datetime(MAX(sm.created_at)) < datetime('now', '-' || ? || ' minutes')
            """,
            (idle_threshold_minutes,),
        )
        return [dict(r) for r in rows] if rows else []

    async def _find_idle_sessions_silent(
        self, db: Database, idle_threshold_minutes: float
    ) -> list[dict]:
        """Silent-mode idle detection.

        Same shape as `_find_idle_sessions`, but the "messages since last
        extraction" anchor reads MAX(ran_at) from memory_extraction_turns
        instead of memory_bulletins.session_range_end.
        """
        rows = await db.fetch_all(
            """
            SELECT
                sm.session_key,
                MAX(sm.created_at) AS last_message_at,
                COALESCE(
                    (SELECT MAX(ran_at) FROM memory_extraction_turns
                     WHERE session_key = sm.session_key),
                    '1970-01-01'
                ) AS active_from,
                COUNT(*) AS message_count
            FROM session_messages sm
            WHERE sm.session_key NOT LIKE 'subagent:%'
              AND datetime(sm.created_at) > datetime(COALESCE(
                (SELECT MAX(ran_at) FROM memory_extraction_turns
                 WHERE session_key = sm.session_key),
                '1970-01-01'
              ))
            GROUP BY sm.session_key
            HAVING datetime(MAX(sm.created_at)) < datetime('now', '-' || ? || ' minutes')
            """,
            (idle_threshold_minutes,),
        )
        return [dict(r) for r in rows] if rows else []

    async def run(self, ctx: AppContext) -> None:
        from bob_server.services.memory import MemoryService

        idle_threshold = ctx.settings.session_summary_idle_minutes
        mode = ctx.settings.memory_extraction.mode

        if mode == "silent":
            idle_sessions = await self._find_idle_sessions_silent(ctx.db, idle_threshold)
        else:
            idle_sessions = await self._find_idle_sessions(ctx.db, idle_threshold)

        if not idle_sessions:
            return

        svc = MemoryService(ctx)

        for session in idle_sessions:
            session_key = session["session_key"]
            try:
                if mode == "silent":
                    result = await svc.run_silent_turn_extraction(session_key)
                    logger.info(
                        "Silent extraction %s for session %s: %s claim(s)",
                        result.get("status"), session_key,
                        result.get("claims_created", 0),
                    )
                else:
                    workspace = ctx.settings.harness.workspace_dir
                    result = await svc.generate_session_bulletins(
                        workspace,
                        session_key,
                        active_from=session["active_from"],
                        run_dream=False,
                    )
                    n = result.get("bulletins_generated", 0)
                    if n:
                        logger.info(
                            "Generated %d bulletin(s) for session %s (%d messages)",
                            n, session_key, session["message_count"],
                        )
            except Exception:
                logger.exception(
                    "Failed to process session %s",
                    session_key,
                )


class CallCleanupTask:
    """Delete old phone call recordings and database records."""

    name = "call_cleanup"

    async def run(self, ctx: AppContext) -> None:
        global _last_call_cleanup
        settings = ctx.settings
        if not settings.phone.enabled:
            return

        # Only run once per 24 hours
        now = datetime.now(timezone.utc)
        if _last_call_cleanup and (now - _last_call_cleanup) < timedelta(hours=24):
            return

        max_age_days = settings.phone.call_recording_max_age_days
        cutoff = (now - timedelta(days=max_age_days)).isoformat()

        old_calls = await ctx.db.fetch_all(
            "SELECT id, recording_path FROM phone_calls WHERE completed_at < ?",
            (cutoff,),
        )
        if not old_calls:
            _last_call_cleanup = now
            return

        for call in old_calls:
            if call["recording_path"]:
                audio_path = settings.data_dir / "calls" / call["recording_path"]
                if audio_path.exists():
                    audio_path.unlink()
            await ctx.db.execute(
                "DELETE FROM phone_call_exchanges WHERE call_id = ?",
                (call["id"],),
            )
            await ctx.db.execute(
                "DELETE FROM phone_calls WHERE id = ?",
                (call["id"],),
            )

        _last_call_cleanup = now
        logger.info("Cleaned up %d old phone call(s)", len(old_calls))


class LLMCallStalenessTask:
    """Mark LLM calls stuck in 'running' status as failed."""

    name = "llm_call_staleness"

    STALE_MINUTES = 30

    async def run(self, ctx: AppContext) -> None:
        count = await ctx.db.execute(
            "UPDATE llm_call_log SET status = 'failed', error_message = 'Stale running call — timed out' "
            "WHERE status = 'running' AND created_at < datetime('now', ?)",
            (f'-{self.STALE_MINUTES} minutes',),
        )
        if count:
            logger.warning("Marked %d stale LLM call(s) as failed", count)
