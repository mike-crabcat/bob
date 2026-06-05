"""Registerable background tasks for the heartbeat loop."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

from cyborg_server.config import Settings
from cyborg_server.context import AppContext
from cyborg_server.database import Database


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

        from cyborg_server.services.agentmail_client import AgentMailClient
        from cyborg_server.services.email_polling_service import EmailPollingService

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

        from cyborg_server.services.agentmail_client import AgentMailClient
        from cyborg_server.services.email_polling_service import EmailPollingService

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
                     WHERE source_type = 'session' AND source_id = sm.session_key
                       AND session_range_end != ''),
                    '1970-01-01'
                ) AS active_from,
                COUNT(*) AS message_count
            FROM session_messages sm
            WHERE sm.created_at > COALESCE(
                (SELECT MAX(session_range_end) FROM memory_bulletins
                 WHERE source_type = 'session' AND source_id = sm.session_key
                   AND session_range_end != ''),
                '1970-01-01'
            )
            GROUP BY sm.session_key
            HAVING MAX(sm.created_at) < datetime('now', '-' || ? || ' minutes')
            """,
            (idle_threshold_minutes,),
        )
        return [dict(r) for r in rows] if rows else []

    async def _get_messages_for_period(
        self, db: Database, session_key: str, active_from: str, active_to: str
    ) -> list[dict]:
        rows = await db.fetch_all(
            """SELECT role, content, sender_id, created_at FROM session_messages
               WHERE session_key = ? AND created_at > ? AND created_at <= ?
                 AND role IN ('user', 'assistant')
               ORDER BY created_at ASC""",
            (session_key, active_from, active_to),
        )
        return [dict(r) for r in rows] if rows else []

    async def _get_participant_name_map(
        self, db: Database, session_key: str
    ) -> dict[str, str]:
        rows = await db.fetch_all(
            """SELECT contact_id, identifier, display_name FROM session_participants
               WHERE session_key = ?""",
            (session_key,),
        )
        result: dict[str, str] = {}
        for r in rows:
            name = r["display_name"]
            if not name:
                continue
            if r["contact_id"]:
                result[r["contact_id"]] = name
        return result

    async def run(self, ctx: AppContext) -> None:
        idle_threshold = ctx.settings.session_summary_idle_minutes
        idle_sessions = await self._find_idle_sessions(ctx.db, idle_threshold)

        if not idle_sessions:
            return

        from cyborg_server.services.memory import MemoryService
        from cyborg_server.services.memory.bulletin_generator import (
            build_generator_input,
            generate_bulletins,
        )
        from cyborg_server.services.memory.channels import (
            derive_visibility,
            resolve_channel_id,
        )
        from cyborg_server.services.llm_dispatch import LLMDispatchService

        mem_svc = MemoryService(ctx)
        llm = LLMDispatchService(ctx)

        for session in idle_sessions:
            try:
                messages = await self._get_messages_for_period(
                    ctx.db,
                    session["session_key"],
                    session["active_from"],
                    session["last_message_at"],
                )
                if not messages:
                    continue

                contact_to_name = await self._get_participant_name_map(
                    ctx.db, session["session_key"],
                )

                gen_input = build_generator_input(
                    session_key=session["session_key"],
                    messages=[
                        {
                            "sender_contact_id": m.get("sender_id", "assistant"),
                            "timestamp": m.get("created_at", ""),
                            "content": (m.get("content") or "")[:500],
                        }
                        for m in messages[-50:]
                    ],
                    participants=[
                        {"id": cid, "name": name}
                        for cid, name in contact_to_name.items()
                    ],
                )

                bulletin_texts = await generate_bulletins(llm, gen_input)
                if not bulletin_texts:
                    continue

                channel_id = resolve_channel_id(session["session_key"])
                visibility = derive_visibility(session["session_key"])

                for text in bulletin_texts:
                    try:
                        await mem_svc.write_bulletin(
                            ctx.settings.harness.workspace_dir,
                            channel_id=channel_id,
                            source_type="session",
                            source_id=session["session_key"],
                            content=text,
                            visibility=visibility,
                            occurred_at=session["last_message_at"],
                            session_range_start=session["active_from"],
                            session_range_end=session["last_message_at"],
                        )
                    except Exception:
                        logger.exception(
                            "Failed to write bulletin for session %s",
                            session["session_key"],
                        )

                logger.info(
                    "Generated %d bulletin(s) for session %s (%d messages)",
                    len(bulletin_texts), session["session_key"], session["message_count"],
                )
            except Exception:
                logger.exception(
                    "Failed to process session %s",
                    session["session_key"],
                )

        # Run the memory dream to curate bulletins into claims and entities
        try:
            from uuid import uuid4
            import json as _json

            result = await mem_svc.run_dream(ctx.settings.harness.workspace_dir)
            if result["status"] != "empty":
                await ctx.db.execute(
                    "INSERT INTO memory_dream_log "
                    "(id, bulletins_processed, entries_created, bulletin_slugs, "
                    "operations_json, raw_response, duration_seconds, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid4()), result["bulletins_processed"],
                     result.get("entity_ops", 0),
                     _json.dumps(result.get("bulletin_slugs", [])),
                     _json.dumps(result.get("operations", [])),
                     None, result.get("duration_seconds"), result["status"]),
                )
        except Exception:
            logger.exception("Memory dream process failed")


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
