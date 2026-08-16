"""Prospective pass: review prior items, apply lifecycle decisions."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from bob_server.context import AppContext
from bob_server.services.base import BaseService, iso_utc, json_loads, utcnow
from bob_server.services.dream.models import (
    PLAN_ACTIVE_STATUSES,
    RESOLUTION_ACTIVE_STATUSES,
    Evidence,
)
from bob_server.services.dream.review import ReviewService

logger = logging.getLogger(__name__)

_MAX_ITEMS_PER_PASS = 25
_MAX_MESSAGES_PER_ITEM = 30


def _parse_iso(value: str | None):
    """Tz-aware parse; naive stamps (SQLite datetime('now')) are assumed UTC."""
    from datetime import datetime, timezone

    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class ProspectiveService(BaseService):
    """Reviews non-terminal items from earlier runs with fresh evidence."""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        from bob_server.services.dream.store import DreamStore

        self.store = DreamStore(ctx)

    async def run(self, *, run_id: str, settings: Any) -> dict[str, Any]:
        """Returns stats + list of plan ids that decided to spend their follow-up."""
        stats: dict[str, Any] = {"items_reviewed": 0, "decisions": [], "reannounce": []}
        plans = await self.store.list_plans(list(PLAN_ACTIVE_STATUSES), limit=_MAX_ITEMS_PER_PASS)
        resolutions = await self.store.list_resolutions(
            [s for s in RESOLUTION_ACTIVE_STATUSES if s != "draft"], limit=_MAX_ITEMS_PER_PASS
        )
        items: list[dict] = []
        for p in plans:
            items.append({"item_type": "plan", **p})
        for r in resolutions:
            items.append({"item_type": "resolution", **r})
        if not items:
            return stats

        contexts = []
        for item in items:
            contexts.append(await self._item_context(item))

        from bob_server.services.dream.prompts import PROSPECTIVE_SYSTEM

        user_prompt = self._build_prompt(contexts)
        review = ReviewService(self.ctx)
        llm_mod = await self._llm()
        raw = await review._chat_json(
            llm_mod,
            system=PROSPECTIVE_SYSTEM + f"\n\nToday: {utcnow().strftime('%Y-%m-%d')}",
            user=user_prompt,
            call_category="dream_prospective",
            session_key="",
            model=self.ctx.settings.openai.get_memory_model(),
        )
        decisions = (raw or {}).get("decisions", []) or []
        by_id = {item["id"]: item for item in items}

        for dec in decisions:
            if not isinstance(dec, dict):
                continue
            item_id, action = str(dec.get("item_id", "")), str(dec.get("action", ""))
            item = by_id.get(item_id)
            if item is None or action not in (
                "complete", "actioned", "expire", "flag_stalled", "reannounce",
                "kept", "dropped", "keep",
            ):
                continue
            reason = str(dec.get("reason", ""))[:500]
            applied = await self._apply(item, action, reason, run_id=run_id, settings=settings)
            entry = {"item_type": item["item_type"], "item_id": item_id, "action": action, "reason": reason, "applied": applied}
            stats["decisions"].append(entry)
            if applied and action == "reannounce":
                stats["reannounce"].append(item_id)

        # Code-side stale sweep: resolutions not re-observed for N runs (approximated
        # as N * interval of quiet time on last_seen_at).
        stale_horizon = timedelta(
            minutes=settings.resolution_stale_runs * max(settings.interval_minutes, 1)
        )
        for r in resolutions:
            if r["status"] in ("open", "in_program"):
                last = r.get("last_seen_at") or r.get("first_seen_at") or ""
                last_dt = _parse_iso(last)
                if last_dt is not None and (utcnow() - last_dt) > stale_horizon:
                    await self.store.set_resolution_status(
                        r["id"], "stale",
                        evidence=Evidence(kind="stale", note="not re-observed in window", run_id=run_id),
                    )
                    stats["decisions"].append(
                        {"item_type": "resolution", "item_id": r["id"], "action": "stale",
                         "reason": "code-side sweep: quiet beyond horizon", "applied": True}
                    )

        stats["items_reviewed"] = len(items)
        return stats

    # ------------------------------------------------------------- contexts

    async def _item_context(self, item: dict) -> dict:
        evidence = json_loads(item.get("evidence_json"), [])
        sessions = {e.get("session_key") for e in evidence if e.get("session_key")}
        for link in await self.store.links_for_item(item["item_type"], item["id"]):
            if link.get("session_key"):
                sessions.add(link["session_key"])
        last_touch = item.get("updated_at") or item.get("last_seen_at") or item.get("created_at") or ""
        recent: list[str] = []
        engaged_sessions: list[str] = []
        for sk in list(sessions)[:3]:
            msgs = await self.store.user_messages_since(sk, last_touch) if last_touch else []
            if msgs:
                engaged_sessions.append(sk)
            for m in msgs[:_MAX_MESSAGES_PER_ITEM]:
                content = (m.get("content") or "").replace("\n", " ")[:200]
                recent.append(f"{sk} [{m.get('created_at', '')}]: {content}")
        progress_entries = [e for e in evidence if e.get("kind") == "progress"]
        return {
            "item_type": item["item_type"],
            "id": item["id"],
            "status": item["status"],
            "title": item.get("title", ""),
            "summary": (item.get("what_was_discussed") or item.get("behaviour") or "")[:300],
            "due_hint": item.get("due_hint") or "",
            "success_signal": item.get("success_signal") or "",
            "announced_at": item.get("announced_at") or "",
            "reannounced_at": item.get("reannounced_at") or "",
            "progress_count": len(progress_entries),
            "engaged_sessions": engaged_sessions,
            "recent_messages": recent[:_MAX_MESSAGES_PER_ITEM],
        }

    def _build_prompt(self, contexts: list[dict]) -> str:
        parts = ["Items to review:\n"]
        for c in contexts:
            bits = [f"- [{c['item_type']}] {c['id']} status={c['status']} title={c['title']!r}"]
            if c["summary"]:
                bits.append(f"  about: {c['summary']}")
            if c["due_hint"]:
                bits.append(f"  due_hint: {c['due_hint']}")
            if c["success_signal"]:
                bits.append(f"  success_signal: {c['success_signal']}")
            if c["announced_at"]:
                bits.append(f"  announced: {c['announced_at']} (follow-up spent: {bool(c['reannounced_at'])})")
            if c["item_type"] == "plan":
                bits.append(f"  progress entries: {c['progress_count']}")
            bits.append(f"  sessions with user engagement since last touch: {len(c['engaged_sessions'])}")
            if c["recent_messages"]:
                bits.append("  recent user messages:")
                bits.extend(f"    {m}" for m in c["recent_messages"])
            parts.append("\n".join(bits))
        parts.append("\nDecide one action per item. STRICT JSON only.")
        return "\n".join(parts)

    # ---------------------------------------------------------------- apply

    async def _apply(self, item: dict, action: str, reason: str, *, run_id: str, settings: Any) -> bool:
        item_type, item_id = item["item_type"], item["id"]
        status = item["status"]

        if item_type == "plan":
            if action == "complete" and status in PLAN_ACTIVE_STATUSES:
                await self.store.set_plan_status(
                    item_id, "completed",
                    evidence=Evidence(kind="completed", note=reason, run_id=run_id),
                )
                return True
            if action == "actioned" and status in ("draft", "proposed", "approved"):
                await self.store.set_plan_status(
                    item_id, "actioned",
                    evidence=Evidence(kind="progress", note=reason, run_id=run_id),
                )
                return True
            if action == "expire":
                # Engagement guard: never expire a plan people are still engaging with.
                engaged = await self._has_engagement(item)
                if engaged:
                    return False
                await self.store.set_plan_status(
                    item_id, "expired",
                    evidence=Evidence(kind="expired", note=reason, run_id=run_id),
                )
                return True
            if action == "flag_stalled" and status == "actioned":
                await self.store.append_plan_evidence(
                    item_id, Evidence(kind="stalled", note=reason, run_id=run_id)
                )
                return True
            if action == "reannounce" and status == "approved":
                if item.get("reannounced_at") or not item.get("announced_at"):
                    return False
                ann_dt = _parse_iso(item.get("announced_at"))
                if ann_dt is None:
                    return False
                if (utcnow() - ann_dt) < timedelta(days=settings.reannounce_after_days):
                    return False
                await self.db.execute(
                    "UPDATE dream_plans SET reannounced_at = ?, updated_at = ? WHERE id = ?",
                    (iso_utc(), iso_utc(), item_id),
                )
                await self.store.append_plan_evidence(
                    item_id, Evidence(kind="reannounce", note=reason, run_id=run_id)
                )
                return True
            return False

        # resolutions
        if action == "kept":
            consecutive = await self._consecutive_kept_signals(item_id)
            if consecutive + 1 >= settings.resolution_kept_consecutive_runs:
                await self.store.set_resolution_status(
                    item_id, "kept",
                    evidence=Evidence(kind="kept", note=reason, run_id=run_id),
                )
            else:
                await self.store.set_resolution_status(
                    item_id, item["status"],
                    evidence=Evidence(kind="kept_signal", note=reason, run_id=run_id),
                )
            return True
        if action == "dropped":
            await self.store.set_resolution_status(
                item_id, "dropped",
                evidence=Evidence(kind="dropped", note=reason, run_id=run_id),
            )
            return True
        return action == "keep"

    async def _has_engagement(self, item: dict) -> bool:
        evidence = json_loads(item.get("evidence_json"), [])
        sessions = {e.get("session_key") for e in evidence if e.get("session_key")}
        last_touch = item.get("announced_at") or item.get("updated_at") or item.get("created_at") or ""
        if not last_touch:
            return False
        for sk in sessions:
            msgs = await self.store.user_messages_since(sk, last_touch)
            if msgs:
                return True
        return False

    async def _consecutive_kept_signals(self, item_id: str) -> int:
        row = await self.db.fetch_one(
            "SELECT evidence_json FROM dream_resolutions WHERE id = ?", (item_id,)
        )
        evidence = json_loads(row["evidence_json"] if row else None, [])
        count = 0
        for entry in reversed(evidence):
            kind = entry.get("kind", "")
            if kind == "kept_signal":
                count += 1
            else:
                break
        return count

    async def _llm(self):
        from bob_server.services.llm_dispatch import LLMDispatchService

        return LLMDispatchService(self.ctx)
