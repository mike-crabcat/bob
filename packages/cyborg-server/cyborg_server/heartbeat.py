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


async def _generate_session_bulletin(
    ctx: AppContext,
    session: dict,
    messages: list[dict],
    contact_to_name: dict[str, str],
) -> dict | None:
    """Generate a single bulletin for an idle session.

    Returns the validated bulletin data dict if one was written, else None.
    Loads the full contacts DB as ``known_entities`` so the LLM uses canonical
    contact IDs rather than inventing name-slug / unresolved- variants.
    """
    from cyborg_server.services.memory import MemoryService
    from cyborg_server.services.memory.bulletin_generator import (
        build_generator_input,
        generate_bulletin,
        validate_draft_bulletin,
    )
    from cyborg_server.services.memory.contact_directory import ContactDirectory
    from cyborg_server.services.llm_dispatch import LLMDispatchService

    transcript_text = "\n".join(
        f"[{m.get('sender_id', m['role'])}] {m['content'][:500]}"
        for m in messages[-50:]
    )

    directory = await ContactDirectory.load(ctx.db)
    gen_input = build_generator_input(
        session_key=session["session_key"],
        transcript_start=session["active_from"],
        transcript_end=session["last_message_at"],
        transcript_text=transcript_text,
        contact_ids=list(contact_to_name.keys()),
        known_entities=directory.as_known_entities(),
    )
    llm = LLMDispatchService(ctx)
    draft = await generate_bulletin(llm, gen_input)
    is_valid, data = validate_draft_bulletin(draft)
    if not is_valid or not data.get("create_bulletin"):
        return None

    mem_svc = MemoryService(ctx)
    await mem_svc.write_bulletin(
        ctx.settings.harness.workspace_dir,
        channel_id=gen_input.channel_id,
        source_type="session_transcript_range",
        source_id=session["session_key"],
        session_id=data.get("session_id", session["session_key"]),
        transcript_range_id=data.get("transcript_range_id", ""),
        visibility=data.get("visibility", gen_input.visibility),
        scope=data.get("scope", gen_input.scope),
        entities=data.get("entities", {}),
        memory_types=data.get("memory_types", []),
        confidence=data.get("confidence", "medium"),
        requires_review=data.get("requires_review", False),
        review_reasons=data.get("review_reasons", []),
        content=draft,
    )
    return data


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
                    message_count=session["message_count"],
                    model_used=ctx.settings.openai.default_model,
                )

                # Generate bulletin directly from transcript
                try:
                    await _generate_session_bulletin(ctx, session, messages, contact_to_name)
                except Exception:
                    logger.exception(
                        "Bulletin generation failed for session %s",
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
            from cyborg_server.services.memory import MemoryService
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
