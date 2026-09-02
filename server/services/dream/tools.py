"""Session-bound plan tools: participants adjust plans conversationally.

Enforcement: plans are resolved via dream_item_links restricted to the current
session key — a plan is only reachable from a session it is linked to, so
participants (party to the originating conversation) can act and outsiders
cannot. Operator override lives in the dashboard/CLI, not here.
"""

from __future__ import annotations

import json

from server.context import AppContext
from server.services.tools import Tool, tool

_ACTIVE_PLAN_STATUSES = ("draft", "proposed", "approved", "actioned")


def make_dream_tools(ctx: AppContext, *, session_key: str) -> list[Tool]:
    from server.services.dream.store import DreamStore

    store = DreamStore(ctx)

    async def _session_plans() -> list[dict]:
        rows = await store.items_for_session(session_key, item_type="plan")
        plans = []
        for r in rows:
            plan = await store.get_plan(r["item_id"])
            if plan and plan["status"] in _ACTIVE_PLAN_STATUSES:
                plans.append(plan)
        return plans

    async def _resolve(plan_id: str | None) -> dict | None:
        plans = await _session_plans()
        if plan_id:
            return next((p for p in plans if p["id"] == plan_id), None)
        return plans[0] if len(plans) == 1 else None

    def _plan_line(p: dict) -> str:
        return (
            f"{p['id']} [{p['status']}] {p['title']} — next step: {p['proposed_action']}"
            + (f" (due: {p['due_hint']})" if p.get("due_hint") else "")
        )

    @tool
    async def list_plans() -> str:
        """List open plans for this conversation (id, status, next step)."""
        plans = await _session_plans()
        if not plans:
            return json.dumps({"plans": [], "note": "no open plans for this session"})
        return json.dumps({"plans": [_plan_line(p) for p in plans]})

    @tool
    async def plan_cancel(reason: str, plan_id: str = "") -> str:
        """Cancel an open plan for this conversation. Use when someone says it's off,
        not wanted, or already handled elsewhere. `reason` should quote or paraphrase them."""
        from server.services.dream.models import Evidence
        from server.services.base import utcnow

        plan = await _resolve(plan_id or None)
        if plan is None:
            return json.dumps({"ok": False, "error": "plan not found in this session (or ambiguous — pass plan_id from list_plans)"})
        await store.set_plan_status(
            plan["id"], "dismissed",
            evidence=Evidence(kind="cancelled", note=reason[:500], at=utcnow().isoformat(), session_key=session_key),
        )
        return json.dumps({"ok": True, "cancelled": plan["id"]})

    @tool
    async def plan_complete(plan_id: str = "") -> str:
        """Mark an open plan completed. Use when someone says it's done/sorted/arranged."""
        from server.services.dream.models import Evidence
        from server.services.base import utcnow

        plan = await _resolve(plan_id or None)
        if plan is None:
            return json.dumps({"ok": False, "error": "plan not found in this session (or ambiguous — pass plan_id from list_plans)"})
        await store.set_plan_status(
            plan["id"], "completed",
            evidence=Evidence(kind="completed", note="reported done by participant", at=utcnow().isoformat(), session_key=session_key),
        )
        return json.dumps({"ok": True, "completed": plan["id"]})

    @tool
    async def plan_update(
        plan_id: str = "",
        due_hint: str = "",
        proposed_action: str = "",
        assistance_method: str = "",
        progress: str = "",
    ) -> str:
        """Amend an open plan for this conversation. Update fields someone changed
        (new date, different plan). `progress` records a concrete step just taken and
        marks the plan actioned. Empty strings leave fields unchanged."""
        from server.services.dream.models import Evidence
        from server.services.base import utcnow

        plan = await _resolve(plan_id or None)
        if plan is None:
            return json.dumps({"ok": False, "error": "plan not found in this session (or ambiguous — pass plan_id from list_plans)"})
        if not any([due_hint, proposed_action, assistance_method, progress]):
            return json.dumps({"ok": False, "error": "nothing to update"})
        await store.update_plan_fields(
            plan["id"],
            due_hint=due_hint or None,
            proposed_action=proposed_action or None,
            assistance_method=assistance_method or None,
        )
        if progress:
            await store.set_plan_status(
                plan["id"], "actioned",
                evidence=Evidence(kind="progress", note=progress[:500], at=utcnow().isoformat(), session_key=session_key),
            )
        else:
            await store.append_plan_evidence(
                plan["id"],
                Evidence(kind="amended", note="; ".join(filter(None, [due_hint and f"due={due_hint}", proposed_action and "action changed"])), at=utcnow().isoformat(), session_key=session_key),
            )
        return json.dumps({"ok": True, "updated": plan["id"]})

    return [list_plans, plan_cancel, plan_complete, plan_update]
