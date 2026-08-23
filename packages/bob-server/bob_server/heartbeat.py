"""Registerable background tasks for the heartbeat loop."""

from __future__ import annotations

import asyncio
import json
import logging
import time
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
    """Detect idle sessions and trigger silent-turn memory extraction."""

    name = "session_idle_summary"

    async def _find_idle_sessions(
        self, db: Database, idle_threshold_minutes: float
    ) -> list[dict]:
        """Find sessions with messages newer than the last silent extraction turn."""
        rows = await db.fetch_all(
            """
            SELECT
                sm.conversation_id AS session_key,
                MAX(sm.created_at) AS last_message_at,
                COALESCE(
                    (SELECT MAX(ran_at) FROM memory_extraction_turns
                     WHERE session_key = sm.conversation_id),
                    '1970-01-01'
                ) AS active_from,
                COUNT(*) AS message_count
            FROM messages sm
            WHERE sm.conversation_id NOT LIKE 'subagent:%'
              AND datetime(sm.created_at) > datetime(COALESCE(
                (SELECT MAX(ran_at) FROM memory_extraction_turns
                 WHERE session_key = sm.conversation_id),
                '1970-01-01'
              ))
            GROUP BY sm.conversation_id
            HAVING datetime(MAX(sm.created_at)) < datetime('now', '-' || ? || ' minutes')
            """,
            (idle_threshold_minutes,),
        )
        return [dict(r) for r in rows] if rows else []

    async def run(self, ctx: AppContext) -> None:
        from bob_server.services.memory import MemoryService

        idle_threshold = ctx.settings.session_summary_idle_minutes
        idle_sessions = await self._find_idle_sessions(ctx.db, idle_threshold)
        if not idle_sessions:
            return

        svc = MemoryService(ctx)

        for session in idle_sessions:
            session_key = session["session_key"]
            try:
                result = await svc.run_silent_turn_extraction(session_key)
                logger.info(
                    "Silent extraction %s for session %s: %s claim(s)",
                    result.get("status"), session_key,
                    result.get("claims_created", 0),
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
                "DELETE FROM phone_calls WHERE id = ?",
                (call["id"],),
            )

        _last_call_cleanup = now
        logger.info("Cleaned up %d old phone call(s)", len(old_calls))


_last_llm_log_redaction: datetime | None = None


class LlmLogRetentionTask:
    """Redact heavy payloads from llm_call_log rows older than 30 days.

    Rows and metrics (tokens, latency, status, model) are kept forever;
    prompts, messages, responses, and tool blocks are stripped. This keeps
    telemetry from dominating database size (it once reached 2.4GB of a
    2.5GB file) without losing usage history. Bob3 plan Phase 0, decision 7.
    """

    name = "llm_log_retention"
    payload_max_age_days = 30

    async def run(self, ctx: AppContext) -> None:
        global _last_llm_log_redaction
        now = datetime.now(timezone.utc)
        if _last_llm_log_redaction and (now - _last_llm_log_redaction) < timedelta(hours=24):
            return
        _last_llm_log_redaction = now

        cutoff = (now - timedelta(days=self.payload_max_age_days)).isoformat()
        redacted = await ctx.db.execute(
            """UPDATE llm_call_log
               SET system_prompt = '', user_message = '', messages_json = NULL,
                   response_text = '', tools_json = NULL, tool_blocks_json = NULL
               WHERE created_at < ?
                 AND (length(system_prompt) > 0 OR length(user_message) > 0
                      OR messages_json IS NOT NULL OR length(response_text) > 0
                      OR tools_json IS NOT NULL OR tool_blocks_json IS NOT NULL)""",
            (cutoff,),
        )
        if redacted:
            logger.info("Redacted payloads from %d llm_call_log row(s)", redacted)


_last_event_log_reconcile: datetime | None = None


class EventLogReconciliationTask:
    """Daily audit: compare legacy channel stores against event_log (Bob3 Phase I).

    Audit-only — logs unmatched counts, never writes. Compares the last 24h of
    whatsapp session_messages and email_messages against event_log appends,
    scoped to rows created after the first event of each source (i.e. after
    the ingress-append deploy). Count divergence is expected signal: e.g.
    WhatsApp has no in-service dedup while event_log dedups on wa_message_id.
    """

    name = "event_log_reconciliation"

    async def run(self, ctx: AppContext) -> None:
        global _last_event_log_reconcile
        now = datetime.now(timezone.utc)
        if _last_event_log_reconcile and (now - _last_event_log_reconcile) < timedelta(hours=24):
            return
        _last_event_log_reconcile = now

        since = (now - timedelta(hours=24)).isoformat()
        for source, legacy_sql in (
            ("whatsapp",
             """SELECT COUNT(*) AS n FROM messages
                WHERE role = 'user' AND channel = 'whatsapp' AND created_at >= ?"""),
            ("email",
             """SELECT COUNT(*) AS n FROM email_messages m
                JOIN email_inboxes i ON i.id = m.inbox_id
                WHERE m.created_at >= ? AND m.sender_email != i.email_address"""),
        ):
            first = await ctx.db.fetch_one(
                "SELECT MIN(recorded_at) AS t FROM event_log WHERE source = ?", (source,))
            baseline = (first or {}).get("t")
            if not baseline:
                continue  # no events yet for this source; nothing to reconcile
            window_start = max(since, baseline)
            legacy = await ctx.db.fetch_one(legacy_sql, (window_start,))
            events = await ctx.db.fetch_one(
                "SELECT COUNT(*) AS n FROM event_log WHERE source = ? AND recorded_at >= ?",
                (source, window_start))
            legacy_n = (legacy or {}).get("n", 0) or 0
            events_n = (events or {}).get("n", 0) or 0
            if legacy_n != events_n:
                logger.warning(
                    "event_log reconciliation [%s]: legacy=%d events=%d since %s",
                    source, legacy_n, events_n, window_start)
            else:
                logger.info(
                    "event_log reconciliation [%s]: OK (%d rows) since %s",
                    source, legacy_n, window_start)


_last_attention_agreement: datetime | None = None


class AttentionShadowAgreementTask:
    """Daily Phase III soak metric: shadow decisions vs live dispatcher.

    A shadow ACT agrees when the live dispatcher produced an assistant
    message in that session within 10 minutes of the stimulus; a WAIT agrees
    when it did not. Logged as telemetry only — the ≥90% ACT-agreement
    cutover gate (plan Phase III exit) reads these lines over the soak week.
    """

    name = "attention_shadow_agreement"
    _reply_window_minutes = 10

    async def run(self, ctx: AppContext) -> None:
        global _last_attention_agreement
        now = datetime.now(timezone.utc)
        if _last_attention_agreement and (now - _last_attention_agreement) < timedelta(hours=24):
            return
        _last_attention_agreement = now

        since = (now - timedelta(hours=24)).isoformat()
        rows = await ctx.db.fetch_all(
            "SELECT id, session_key, decision, created_at FROM attention_shadow "
            "WHERE created_at >= ? ORDER BY id", (since,))
        if not rows:
            return
        stats = {"ACT": {"agree": 0, "total": 0}, "WAIT": {"agree": 0, "total": 0}}
        for r in rows:
            decision = r["decision"]
            if decision not in stats:
                continue
            replied = await ctx.db.fetch_one(
                """SELECT 1 AS x FROM messages
                   WHERE conversation_id = COALESCE(
                       (SELECT conversation_id FROM bindings WHERE session_key = ?), ?)
                     AND role = 'assistant'
                     AND datetime(created_at) > datetime(?)
                     AND datetime(created_at) <= datetime(?, ?)
                   LIMIT 1""",
                (r["session_key"], r["session_key"], r["created_at"], r["created_at"],
                 f"+{self._reply_window_minutes} minutes"))
            live_acted = replied is not None
            agree = (decision == "ACT") == live_acted
            stats[decision]["total"] += 1
            if agree:
                stats[decision]["agree"] += 1
        for decision, s in stats.items():
            if s["total"]:
                pct = 100.0 * s["agree"] / s["total"]
                logger.info(
                    "attention shadow agreement [%s]: %.1f%% (%d/%d) over last 24h",
                    decision, pct, s["agree"], s["total"])


_last_memory_reconcile: datetime | None = None


class MemoryReconciliationTask:
    """Heartbeat-triggered reconciliation of entities touched in the last 24h.

    Replaces the post-dream reconciliation trigger that was removed with the
    dream pipeline. Finds entities with claims/entities written today, filters
    by min_interval_hours backoff, and runs reconcile_entity on each. Throttled
    to 1h to avoid hammering the LLM on every heartbeat while still reacting
    to new claims within the hour.
    """

    name = "memory_reconciliation"
    _THROTTLE = timedelta(hours=1)

    async def run(self, ctx: AppContext) -> None:
        global _last_memory_reconcile
        recon = ctx.settings.reconciliation
        if not recon.daily_batch_enabled:
            return

        now = datetime.now(timezone.utc)
        if _last_memory_reconcile and (now - _last_memory_reconcile) < self._THROTTLE:
            return

        from bob_server.services.memory import MemoryService
        from bob_server.services.memory.reconciliation import filter_due_for_reconciliation

        rows = await ctx.db.fetch_all(
            """
            SELECT entity_id, MAX(touched_at) AS last_touched FROM (
                SELECT subject_id AS entity_id, created_at AS touched_at
                FROM memory_claims
                WHERE status = 'active'
                  AND datetime(created_at) > datetime('now', '-24 hours')
                UNION ALL
                SELECT entity_id, created_at AS touched_at
                FROM memory_entities
                WHERE status = 'active'
                  AND datetime(created_at) > datetime('now', '-24 hours')
            )
            GROUP BY entity_id
            ORDER BY last_touched DESC
            LIMIT ?
            """,
            (recon.daily_batch_max_entities,),
        )
        candidate_ids = [r["entity_id"] for r in rows] if rows else []
        if not candidate_ids:
            _last_memory_reconcile = now
            return

        due = await filter_due_for_reconciliation(ctx.db, candidate_ids, recon.min_interval_hours)
        if not due:
            _last_memory_reconcile = now
            return

        svc = MemoryService(ctx)
        workspace = ctx.settings.harness.workspace_dir
        try:
            result = await svc.reconcile_entities(workspace, entity_ids=due)
            logger.info(
                "MemoryReconciliationTask: %d entit%s checked, %d op(s), %d question(s) raised",
                result.get("entities_checked", 0),
                "y" if result.get("entities_checked", 0) == 1 else "ies",
                result.get("total_ops", 0),
                result.get("total_questions", 0),
            )
        except Exception:
            logger.exception("MemoryReconciliationTask failed")
            return

        _last_memory_reconcile = now


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


class DreamTask:
    """Idle-time dream runs (see dream-v2-plan.md).

    Never blocks the heartbeat loop: gates are checked cheaply, then the run
    is spawned via asyncio.create_task with the runner's single-flight lock
    guarding overlap (established pattern — wakeup pump, email dispatch).
    """

    name = "dream"

    def __init__(self) -> None:
        self._last_check_at: float = 0.0

    async def run(self, ctx: AppContext) -> None:
        settings = ctx.settings.dream
        if not settings.enabled:
            return
        # Self-gate on wall clock so the cadence is robust to interval changes.
        now_mono = time.monotonic()
        if now_mono - self._last_check_at < 300:
            return
        self._last_check_at = now_mono

        from bob_server.services.dream import DreamRunner

        runner = DreamRunner(ctx)

        def _log_failure(task: "asyncio.Task[None]") -> None:
            if not task.cancelled() and task.exception() is not None:
                logger.error("dream background run failed", exc_info=task.exception())

        task = asyncio.create_task(runner.maybe_run(trigger="heartbeat"))
        task.add_done_callback(_log_failure)


class LocationFetchTask:
    """Fetch current location from Home Assistant on a fixed schedule and
    append to location_history table for trip-journal use.

    Self-gates on time.monotonic() so the cadence is robust to changes in
    the heartbeat interval. Uses force_refresh=True to bypass the 2-min
    cache on the HA client — we want fresh data on the schedule.
    """

    name = "location_fetch"

    def __init__(self) -> None:
        self._last_fetch_at: float = 0.0

    async def run(self, ctx: AppContext) -> None:
        ha = ctx.settings.homeassistant
        if not (ha.enabled and ha.history_enabled and ha.history_interval_seconds > 0):
            return
        now_mono = time.monotonic()
        if now_mono - self._last_fetch_at < ha.history_interval_seconds:
            return
        self._last_fetch_at = now_mono

        from bob_server.services.location_tools import _get_ha_client

        client = _get_ha_client(ctx)
        try:
            payload = await client.get_state(ha.device_tracker_entity_id, force_refresh=True)
        except Exception as exc:
            logger.warning("LocationFetchTask: HA query failed: %s", exc)
            return
        if not payload:
            return
        attrs = payload.get("attributes", {}) or {}
        lat = attrs.get("latitude")
        lon = attrs.get("longitude")
        if lat is None or lon is None:
            return  # nothing useful to record

        await ctx.db.execute(
            "INSERT INTO location_history "
            "(fetched_at, device_tracker_entity_id, latitude, longitude, "
            " gps_accuracy, zone_state, battery_level, ha_last_updated, raw_attributes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                ha.device_tracker_entity_id,
                lat,
                lon,
                attrs.get("gps_accuracy"),
                payload.get("state"),
                attrs.get("battery_level"),
                payload.get("last_updated"),
                json.dumps(attrs),
            ),
        )
        logger.debug(
            "LocationFetchTask recorded ping: lat %.4f lon %.4f zone=%s",
            lat, lon, payload.get("state"),
        )


class EffectPumpTask:
    """Deliver due pending effects (Bob3 Phase IV outbox).

    The write path delivers inline; this pump exists for crash leftovers and
    backoff retries. Runs every heartbeat tick — claim_due is a cheap indexed
    query when the outbox is empty.
    """

    name = "effect_pump"

    async def run(self, ctx: AppContext) -> None:
        from bob_server.services.effects import pump_due_effects

        processed = await pump_due_effects(ctx)
        if processed:
            logger.info("effect pump delivered/retried %d effect(s)", processed)


class WakeupPumpTask:
    """Fire due wakeups (Bob3 Phase V): goal deadlines and scheduled wakes."""

    name = "wakeup_pump"

    async def run(self, ctx: AppContext) -> None:
        from bob_server.services.goal_service import pump_due_wakeups

        fired = await pump_due_wakeups(ctx)
        if fired:
            logger.info("wakeup pump fired %d wakeup(s)", fired)


_last_deletion_propagation: datetime | None = None


class DeletionPropagationTask:
    """Daily deletion propagation (Bob3 Phase VII, decision 7): events are
    kept forever, but explicit deletions propagate as payload redaction.
    Event rows referencing a deleted contact keep their identity/ordering
    columns; the payload is replaced with a tombstone."""

    name = "deletion_propagation"

    async def run(self, ctx: AppContext) -> None:
        global _last_deletion_propagation
        now = datetime.now(timezone.utc)
        if _last_deletion_propagation and (now - _last_deletion_propagation) < timedelta(hours=24):
            return
        _last_deletion_propagation = now

        deleted = await ctx.db.fetch_all(
            "SELECT id FROM contacts WHERE deleted_at IS NOT NULL")
        redacted = 0
        for row in deleted:
            redacted += await ctx.db.execute(
                """UPDATE event_log
                   SET payload_json = json_object('redacted', 'contact_deleted')
                   WHERE json_extract(payload_json, '$.contact_id') = ?
                     AND json_extract(payload_json, '$.redacted') IS NULL""",
                (row["id"],),
            )
        if redacted:
            logger.info("deletion propagation redacted %d event payload(s)", redacted)


_last_growth_check: datetime | None = None


class GrowthMonitoringTask:
    """Daily DB growth telemetry (Bob3 Phase VII item 4): logs database size
    and hot-table row counts so growth is visible against the Phase 0
    baseline (journalctl-greppable: 'db growth')."""

    name = "growth_monitoring"

    async def run(self, ctx: AppContext) -> None:
        global _last_growth_check
        now = datetime.now(timezone.utc)
        if _last_growth_check and (now - _last_growth_check) < timedelta(hours=24):
            return
        _last_growth_check = now

        page_count = await ctx.db.fetch_one("PRAGMA page_count")
        page_size = await ctx.db.fetch_one("PRAGMA page_size")
        size_mb = (list(page_count.values())[0] * list(page_size.values())[0]) / (1024 * 1024)
        counts = {}
        for table in ("event_log", "messages", "llm_call_log",
                      "effects", "turns", "goals"):
            row = await ctx.db.fetch_one(f"SELECT COUNT(*) AS n FROM {table}")
            counts[table] = row["n"]
        logger.info("db growth: size=%.1fMB rows=%s", size_mb, counts)
