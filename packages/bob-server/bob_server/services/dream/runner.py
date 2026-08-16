"""DreamRunner — single-flight orchestration of a dream run.

Pipeline: claim run → collect due sessions (per-session cursors, newest first)
→ retrospective review per session → validate/dedup/write items → prospective
review of prior items → announcement flush → journal synthesis → finalise.

Never blocks the heartbeat: the heartbeat task spawns run() via asyncio.create_task.
All LLM passes run on the memory model (D9).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from bob_server.context import AppContext
from bob_server.services.base import BaseService, iso_utc, utcnow
from bob_server.services.dream.models import Evidence, PlanCandidate, ResolutionCandidate

logger = logging.getLogger(__name__)

_run_lock = asyncio.Lock()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:  # SQLite datetime('now') produces naive stamps — assume UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class DreamRunner(BaseService):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        from bob_server.services.dream.store import DreamStore

        self.store = DreamStore(ctx)
        self.settings = ctx.settings.dream

    # ------------------------------------------------------------------ gates

    async def maybe_run(self, trigger: str = "heartbeat") -> dict[str, Any] | None:
        """Gated entry point. Returns the run result, or None if not due."""
        if trigger == "heartbeat":
            if not self.settings.enabled:
                return None
            last = await self.store.last_complete_run()
            if last and last.get("finished_at"):
                last_dt = _parse_iso(last["finished_at"])
                if last_dt and (utcnow() - last_dt) < timedelta(minutes=self.settings.interval_minutes):
                    return None
        if _run_lock.locked():
            logger.info("dream already running; skipping %s trigger", trigger)
            return None
        async with _run_lock:
            return await self.run(trigger)

    # ------------------------------------------------------------------- run

    async def run(self, trigger: str = "heartbeat") -> dict[str, Any]:
        await self.store.sweep_stale_runs()

        lookback = timedelta(days=self.settings.first_run_lookback_days)
        last = await self.store.last_complete_run()
        window_start = (last or {}).get("window_end") or (utcnow() - lookback).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_id = await self.store.create_run(
            trigger=trigger,
            window_start=window_start,
            window_end=iso_utc(),
            model=self.ctx.settings.openai.get_memory_model(),
        )
        stats: dict[str, Any] = {
            "trigger": trigger,
            "sessions": [],
            "resolutions_created": [],
            "plans_created": [],
            "merged": [],
            "suppressed": [],
            "capped_dropped": [],
            "rejected": 0,
            "prospective": {},
            "announce": {},
        }
        logger.info("dream run %s started (%s)", run_id, trigger)

        try:
            await self._retrospective(run_id, stats)
            prospective_stats = await self._prospective(run_id)
            stats["prospective"] = prospective_stats
            await self._announce(stats)
            await self._journal(run_id, stats)
            await self.store.finish_run(run_id, stats=stats, journal_text=stats.pop("_journal_text", ""))
            logger.info("dream run %s complete", run_id)
            return {"run_id": run_id, "stats": stats}
        except Exception as exc:
            logger.exception("dream run %s failed", run_id)
            await self.store.fail_run(run_id, str(exc))
            raise

    # ---------------------------------------------------------- retrospective

    async def _retrospective(self, run_id: str, stats: dict) -> None:
        from bob_server.services.dream.review import ReviewService

        review = ReviewService(self.ctx)
        sessions = await self.store.sessions_due(
            min_new_messages=self.settings.min_new_messages_per_session,
            max_sessions=self.settings.max_sessions_per_run,
            first_run_lookback_days=self.settings.first_run_lookback_days,
        )
        if len(sessions) < self.settings.min_new_sessions and not sessions:
            return

        auto_approve = await self._auto_approve_enabled()

        for session in sessions:
            session_key = session["session_key"]
            cursor_at = (session.get("cursor_at") or "") or None
            messages = await self.store.fetch_session_window(
                session_key, cursor_at,
                lookback_days=self.settings.first_run_lookback_days,
                limit=self.settings.max_transcript_lines,
            )
            if not messages:
                continue

            group_hint = "This session is a WhatsApp group chat." if ":group:" in session_key else ""
            result = await review.review_session(
                session_key=session_key, messages=messages, group_hint=group_hint
            )
            stats["sessions"].append(result["stats"])
            stats["rejected"] += result["stats"].get("rejected_invalid", 0) + result["stats"].get("rejected_bad_json", 0)

            for cand in result["resolutions"]:
                await self._write_resolution(run_id, cand, session_key, stats)
            for cand in result["plans"]:
                await self._write_plan(run_id, cand, session_key, stats, auto_approve=auto_approve)

            # Advance the cursor to the newest message actually reviewed.
            await self.store.set_cursor(session_key, messages[-1]["created_at"], run_id)

    async def _write_resolution(self, run_id: str, cand: ResolutionCandidate, session_key: str, stats: dict) -> None:
        for e in cand.evidence:
            e.run_id = run_id
        if await self._cap_reached(stats, "resolutions_created"):
            stats["capped_dropped"].append({"type": "resolution", "title": cand.title})
            return
        match, terminal, embedding = await self._dedup_lookup("resolution", cand.title, cand.behaviour)
        if match:
            await self.store.merge_resolution(match["id"], cand.evidence, run_id=run_id)
            stats["merged"].append({"type": "resolution", "id": match["id"], "title": cand.title})
            return
        if terminal:
            # Fresh explicit evidence may reopen; otherwise suppress.
            if self._evidence_is_fresh(cand.evidence, terminal):
                await self.store.set_resolution_status(
                    terminal["id"], "open",
                    evidence=Evidence(kind="reopened", note="fresh evidence re-committed", run_id=run_id),
                )
                await self.store.merge_resolution(terminal["id"], cand.evidence, run_id=run_id)
                stats["merged"].append({"type": "resolution", "id": terminal["id"], "title": cand.title, "reopened": True})
            else:
                stats["suppressed"].append({"type": "resolution", "id": terminal["id"], "title": cand.title})
            return
        status = "draft" if self.settings.draft_mode else "open"
        item_id = await self.store.insert_resolution(cand, run_id=run_id, status=status)
        if embedding is not None:
            await self.store.upsert_item_embedding(item_id, embedding)
        await self.store.add_link("resolution", item_id, session_key=session_key)
        stats["resolutions_created"].append({"id": item_id, "title": cand.title})

    async def _write_plan(self, run_id: str, cand: PlanCandidate, session_key: str, stats: dict, *, auto_approve: bool) -> None:
        for e in cand.evidence:
            e.run_id = run_id
        if await self._cap_reached(stats, "plans_created"):
            stats["capped_dropped"].append({"type": "plan", "title": cand.title})
            return
        match, terminal, embedding = await self._dedup_lookup("plan", cand.title, cand.what_was_discussed)
        if match:
            await self.store.merge_plan(match["id"], cand.evidence, run_id=run_id)
            stats["merged"].append({"type": "plan", "id": match["id"], "title": cand.title})
            return
        if terminal:
            if self._evidence_is_fresh(cand.evidence, terminal):
                await self.store.set_plan_status(
                    terminal["id"], "approved", approved_by="auto",
                    evidence=Evidence(kind="reopened", note="fresh evidence re-committed", run_id=run_id),
                )
                await self.store.merge_plan(terminal["id"], cand.evidence, run_id=run_id)
                stats["merged"].append({"type": "plan", "id": terminal["id"], "title": cand.title, "reopened": True})
            else:
                stats["suppressed"].append({"type": "plan", "id": terminal["id"], "title": cand.title})
            return
        # Backlog guard: plans with only old evidence never auto-approve.
        auto_ok = auto_approve and self._evidence_is_fresh(
            cand.evidence, {"ts": (utcnow() - timedelta(days=self.settings.backlog_evidence_days)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        )
        if auto_ok:
            item_id = await self.store.insert_plan(
                cand, run_id=run_id, status="approved", approved_by="auto", session_key=session_key
            )
        else:
            item_id = await self.store.insert_plan(
                cand, run_id=run_id, status="draft" if self.settings.draft_mode else "proposed",
                session_key=session_key,
            )
        if embedding is not None:
            await self.store.upsert_item_embedding(item_id, embedding)
        stats["plans_created"].append({"id": item_id, "title": cand.title, "status": "approved" if auto_ok else "draft/proposed"})

    async def _dedup_lookup(self, item_type: str, title: str, body: str) -> tuple[dict | None, dict | None, list[float] | None]:
        from bob_server.services.memory.embedding import embed_text

        embedding = await embed_text(f"{title}\n{body}"[:2000])
        if embedding is None:
            return None, None, None  # embedding unavailable — fall through to insert
        match, terminal = await self.store.dedup_targets(
            item_type, embedding,
            threshold=self.settings.dedup_distance_threshold,
            terminal_within_days=self.settings.recent_terminal_dedup_days,
        )
        return match, terminal, embedding

    async def _cap_reached(self, stats: dict, key: str) -> bool:
        return len(stats.get(key, [])) >= self.settings.max_new_items_per_type

    @staticmethod
    def _evidence_is_fresh(evidence: list[Evidence], reference: dict) -> bool:
        """Newest evidence citation newer than a reference timestamp."""
        ref = _parse_iso(reference.get("ts") or reference.get("updated_at") or reference.get("last_seen_at"))
        if ref is None:
            return True
        newest = max((_parse_iso(e.at) for e in evidence), default=None)
        return newest is not None and newest > ref

    async def _auto_approve_enabled(self) -> bool:
        from bob_server.services.dream import config as dream_config

        return await dream_config.get_auto_approve_plans(self.db, self.settings.auto_approve_plans)

    # ----------------------------------------------------------- prospective

    async def _prospective(self, run_id: str) -> dict:
        from bob_server.services.dream.prospective import ProspectiveService

        return await ProspectiveService(self.ctx).run(run_id=run_id, settings=self.settings)

    # -------------------------------------------------------------- announce

    async def _announce(self, stats: dict) -> None:
        from bob_server.services.dream.announce import AnnounceService

        svc = AnnounceService(self.ctx)
        stats["announce"] = await svc.flush()
        reannounce_ids = (stats.get("prospective") or {}).get("reannounce", [])
        if reannounce_ids:
            stats["announce"]["reannounced"] = await svc.announce_reannouncements(reannounce_ids)

    # ---------------------------------------------------------------- journal

    async def _journal(self, run_id: str, stats: dict) -> None:
        from bob_server.services.dream.journal import JournalService

        facts = {k: v for k, v in stats.items() if not k.startswith("_")}
        stats["_journal_text"] = await JournalService(self.ctx).synthesise(facts=facts)
