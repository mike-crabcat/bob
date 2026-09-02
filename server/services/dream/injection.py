"""Tier 1 injection: compact "Open plans" block for sessions with linked plans.

Appended to the system prompt of every dispatch in a linked session. Carries
lifecycle state (status, announce age, engagement) so the agent can contextually
re-raise a plan that has sat unactioned — the primary second chance for
announced plans, cheaper and more social than a scheduled cold message.
Gated on dream.enabled; only non-draft plans are ever shown.
"""

from __future__ import annotations

from typing import Any

from bob_server.database import Database

_SHOW_STATUSES = ("proposed", "approved", "actioned")
_MAX_PLANS = 5


async def build_session_plans_prompt(db: Database, session_key: str, *, dream_enabled: bool) -> str:
    if not dream_enabled:
        return ""
    try:
        rows = await db.fetch_all(
            "SELECT item_id FROM dream_item_links WHERE session_key = ? AND item_type = 'plan'",
            (session_key,),
        )
    except Exception:
        return ""  # table missing (pre-migration) — degrade silently
    if not rows:
        return ""

    from bob_server.services.base import json_loads

    plans: list[dict] = []
    for r in rows:
        row = await db.fetch_one("SELECT * FROM dream_plans WHERE id = ?", (r["item_id"],))
        if row and row["status"] in _SHOW_STATUSES:
            plans.append(dict(row))
    if not plans:
        return ""

    plans.sort(key=lambda p: p.get("updated_at") or "")
    lines = [
        "## Open Plans for this Session",
        "",
        "Unfinished business detected in this conversation. Weave it in naturally when",
        "the conversation invites — never force it. If one has sat unactioned a while, you",
        "may gently re-raise it (\"did that ever get booked?\"). When someone says it's done,",
        "off, or changed, use the plan tools (plan_complete / plan_cancel / plan_update).",
        "",
    ]
    for p in plans[:_MAX_PLANS]:
        evidence = json_loads(p.get("evidence_json"), [])
        bits = [f"- {p['id']} [{p['status']}] {p['title']}: {p['what_was_discussed']}"]
        bits.append(f"  next step: {p.get('proposed_action') or '—'}")
        if p.get("assistance_method"):
            bits.append(f"  how you can help: {p['assistance_method']}")
        if p.get("due_hint"):
            bits.append(f"  timeframe: {p['due_hint']}")
        if p.get("announced_at"):
            progress = sum(1 for e in evidence if e.get("kind") == "progress")
            bits.append(
                f"  raised {p['announced_at'][:10]}"
                + (f", follow-up sent {p['reannounced_at'][:10]}" if p.get("reannounced_at") else "")
                + f", progress notes: {progress}"
            )
        lines.extend(bits)
    if len(plans) > _MAX_PLANS:
        lines.append(f"(+{len(plans) - _MAX_PLANS} more — list_plans())")
    return "\n".join(lines)
