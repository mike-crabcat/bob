"""Announcement pipeline: approved plans → one natural message per session.

Guards (all enforced here, not by prompts):
- announce only in the session where the evidence was cited (DM privacy rule)
- WhatsApp sessions with a resolvable chat_id only (v1)
- batch per session per flush; daily cap per session; hot-session defer
- every announcement is recorded via SessionService.add_message with
  synthetic=True (keeps silent-turn extraction from re-ingesting it) and a
  dream_announce metadata marker (what the prospective pass searches for)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from bob_server.context import AppContext
from bob_server.services.base import BaseService, iso_utc, json_dumps, utcnow
from bob_server.services.wake_service import session_key_to_chat_id as _session_key_to_chat_id

logger = logging.getLogger(__name__)


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


class AnnounceService(BaseService):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        from bob_server.services.dream.store import DreamStore

        self.store = DreamStore(ctx)

    async def flush(self) -> dict[str, Any]:
        """Send pending announcements. Idempotent via announced_at guard."""
        settings = self.ctx.settings.dream
        result: dict[str, Any] = {"sessions": 0, "plans_announced": 0, "deferred_hot": 0, "deferred_cap": 0, "skipped": 0}

        pending = await self.store.plans_pending_announce()
        if not pending:
            return result

        by_session: dict[str, list[dict]] = {}
        for plan in pending:
            session_key = await self.store.plan_evidence_session(plan)
            if not session_key or _session_key_to_chat_id(session_key) is None:
                result["skipped"] += 1
                continue
            by_session.setdefault(session_key, []).append(plan)

        bridge = self.ctx.whatsapp_bridge
        for session_key, plans in by_session.items():
            chat_id = _session_key_to_chat_id(session_key)

            # Hot-session defer: don't butt into a live conversation.
            last_inbound = _parse_iso(await self.store.session_last_inbound_at(session_key))
            if last_inbound and (utcnow() - last_inbound) < timedelta(minutes=settings.announce_defer_active_minutes):
                result["deferred_hot"] += len(plans)
                continue

            # Daily cap per session: over-cap plans wait for the next flush.
            if await self.store.announcements_today(session_key) >= settings.announce_daily_cap_per_session:
                result["deferred_cap"] += len(plans)
                continue

            if bridge is None or not getattr(bridge, "connected", False):
                result["deferred_hot"] += 0  # bridge down; keep pending
                logger.info("dream announce: bridge not connected; %d plan(s) stay pending", len(plans))
                continue

            message = await self._compose(session_key, plans)
            try:
                await bridge.send_message(chat_id, message)
            except Exception:
                logger.exception("dream announce: send failed to %s", chat_id)
                continue

            await self._record(session_key, message, plans)
            for plan in plans:
                await self.db.execute(
                    "UPDATE dream_plans SET announced_at = ?, updated_at = ? WHERE id = ?",
                    (iso_utc(), iso_utc(), plan["id"]),
                )
            result["sessions"] += 1
            result["plans_announced"] += len(plans)
            logger.info("dream announce: %d plan(s) announced in %s", len(plans), session_key)

        return result

    async def announce_reannouncements(self, plan_ids: list[str]) -> int:
        """Send the single follow-up for plans whose reannounced_at was set by the
        prospective pass (content composed fresh, tone gentler)."""
        if not plan_ids:
            return 0
        bridge = self.ctx.whatsapp_bridge
        if bridge is None or not getattr(bridge, "connected", False):
            return 0
        settings = self.ctx.settings.dream
        sent = 0
        for plan_id in plan_ids:
            plan = await self.store.get_plan(plan_id)
            if plan is None or plan.get("reannounced_at") is None or plan.get("announced_at") is None:
                continue
            session_key = await self.store.plan_evidence_session(plan)
            if not session_key:
                continue
            chat_id = _session_key_to_chat_id(session_key)
            if chat_id is None:
                continue
            last_inbound = _parse_iso(await self.store.session_last_inbound_at(session_key))
            if last_inbound and (utcnow() - last_inbound) < timedelta(minutes=settings.announce_defer_active_minutes):
                continue
            if await self.store.announcements_today(session_key) >= settings.announce_daily_cap_per_session:
                continue
            message = await self._compose(session_key, [plan], follow_up=True)
            try:
                await bridge.send_message(chat_id, message)
            except Exception:
                logger.exception("dream reannounce: send failed to %s", chat_id)
                continue
            await self._record(session_key, message, [plan])
            sent += 1
        return sent

    async def _compose(self, session_key: str, plans: list[dict], *, follow_up: bool = False) -> str:
        from bob_server.services.dream.prompts import ANNOUNCE_SYSTEM
        from bob_server.services.llm_dispatch import LLMDispatchService

        summaries = []
        for p in plans:
            bits = [f"- {p['title']}: {p['what_was_discussed']}"]
            if p.get("proposed_action"):
                bits.append(f"  next step: {p['proposed_action']}")
            if p.get("due_hint"):
                bits.append(f"  timeframe: {p['due_hint']}")
            summaries.append("\n".join(bits))
        stance = (
            "This is a gentle one-off follow-up on something raised before that went quiet."
            if follow_up
            else "This is the first time raising it."
        )
        llm = LLMDispatchService(self.ctx)
        response = await llm.chat(
            messages=[
                {"role": "system", "content": ANNOUNCE_SYSTEM},
                {"role": "user", "content": f"Session: {session_key}\n{stance}\nPlan summaries:\n" + "\n".join(summaries)},
            ],
            call_category="dream_announce",
            session_key=session_key,
            model=self.ctx.settings.openai.get_memory_model(),
            temperature=0.6,
        )
        return (response or "").strip()[:1500] or "Quick nudge — still happy to help with that thing we mentioned?"

    async def _record(self, session_key: str, message: str, plans: list[dict]) -> None:
        """Record the announcement so future dreams can find it and extraction skips it."""
        from bob_server.services.session_service import SessionService

        await SessionService(self.ctx).add_message(
            session_key,
            "assistant",
            message,
            channel="whatsapp",
            synthetic=True,
            provenance="dream_announcement",
            metadata={"dream_announce": [p["id"] for p in plans]},
        )
