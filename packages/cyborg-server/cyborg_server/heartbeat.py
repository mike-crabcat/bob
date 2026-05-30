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
    """Detect idle sessions and generate summaries for their active periods."""

    name = "session_idle_summary"

    async def run(self, ctx: AppContext) -> None:
        from cyborg_server.services.session_summary_service import SessionSummaryService

        service = SessionSummaryService(ctx)
        idle_threshold = ctx.settings.session_summary_idle_minutes
        idle_sessions = await service.find_idle_sessions(idle_threshold)

        if not idle_sessions:
            return

        for session in idle_sessions:
            try:
                messages = await service.get_messages_for_period(
                    session["session_key"],
                    session["active_from"],
                    session["last_message_at"],
                )
                if not messages:
                    continue

                participants = await service.get_participants_for_period(
                    session["session_key"],
                    session["active_from"],
                    session["last_message_at"],
                )
                name_map = await service.get_participant_name_map(
                    session["session_key"],
                )
                contact_to_name, identifier_to_name = name_map
                result = await service.generate_summary(
                    messages, participants,
                    session["active_from"], session["last_message_at"],
                    contact_to_name=contact_to_name,
                    identifier_to_name=identifier_to_name,
                )
                await service.store_summary(
                    session_key=session["session_key"],
                    active_from=session["active_from"],
                    active_to=session["last_message_at"],
                    summary_text=result["summary_text"],
                    topics=result["topics"],
                    participants=participants,
                    memory_prompts=result["memory_prompts"],
                    message_count=session["message_count"],
                    model_used=ctx.settings.openai.default_model,
                    people_updates=result.get("people_updates"),
                )

                # Trigger memory reflection from conversation summary
                if result.get("memory_prompts") or result.get("people_updates"):
                    try:
                        from cyborg_server.services.memory_service import MemoryService
                        mem_svc = MemoryService(ctx)
                        # General reflection
                        if result.get("memory_prompts"):
                            await mem_svc.reflect_and_update(
                                ctx.settings.harness.workspace_dir,
                                session["session_key"],
                                result["summary_text"],
                                result["memory_prompts"],
                                active_from=session["active_from"],
                                active_to=session["last_message_at"],
                                participants=participants,
                                contact_ids=list(contact_to_name.keys()),
                            )
                        # Person-targeted bulletins
                        people_updates = result.get("people_updates") or {}
                        for contact_ref, facts in people_updates.items():
                            if facts:
                                await mem_svc.reflect_and_update(
                                    ctx.settings.harness.workspace_dir,
                                    session["session_key"],
                                    summary_text="\n".join(f"- {f}" for f in facts),
                                    memory_prompts=facts,
                                    active_from=session["active_from"],
                                    active_to=session["last_message_at"],
                                    participants=participants,
                                    contact_ids=list(contact_to_name.keys()),
                                )
                    except Exception:
                        logger.exception(
                            "Memory reflection failed for session %s",
                            session["session_key"],
                        )
                logger.info(
                    "Session summary generated for %s (%d messages)",
                    session["session_key"], session["message_count"],
                )
            except Exception:
                logger.exception(
                    "Failed to generate summary for session %s",
                    session["session_key"],
                )

        # After all summaries, run the memory dream to curate bulletins
        try:
            from cyborg_server.services.memory_service import MemoryService
            from uuid import uuid4
            import json as _json

            mem_svc = MemoryService(ctx)
            result = await mem_svc.run_dream(ctx.settings.harness.workspace_dir)
            if result["status"] != "empty":
                await ctx.db.execute(
                    "INSERT INTO memory_dream_log "
                    "(id, bulletins_processed, entries_created, bulletin_slugs, "
                    "operations_json, raw_response, duration_seconds, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid4()), result["bulletins_processed"], result["entries_created"],
                     _json.dumps(result["bulletin_slugs"]),
                     _json.dumps(result["operations"]),
                     result.get("raw_response"),
                     result.get("duration_seconds"), result["status"]),
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
