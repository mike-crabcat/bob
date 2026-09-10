"""Utility conversations — self-serve sensation routing
(docs/utility-conversations-plan.md).

Bob requests a behavior (headless conversation + stimulus route + valves),
Mike approves once in his DM, the route goes live. Creation and widening are
owner-approved; firing is autonomous — the same split as steering. This
module owns: spec validation + rendering, the request/edit tools, the
approval follow-through (on_approved flips the route live, on_rejected
deletes the inert rows), and the per-turn charter/model spec used by the
generic wake path.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from server.context import AppContext
from server.services.tools import tool

logger = logging.getLogger(__name__)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
TYPE_PATTERN_RE = re.compile(r"^[a-z0-9.*?\[\]-]+$")
HOURS_RE = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")

# Valve caps set here, not by the requester (plan Part 3): tightening is
# free, loosening is widening — and goes through approval.
MIN_COOLDOWN_S = 600
MAX_BUDGET_PER_HOUR = 6
DEFAULT_COOLDOWN_S = 1800
DEFAULT_BUDGET_PER_HOUR = 6

APPROVAL_TYPE = "sensation_route"


# ── per-turn spec (wake_service seam) ─────────────────────────────────

async def utility_turn_spec(ctx: AppContext,
                            session_key: str) -> tuple[str, str] | None:
    """(charter system block, resolved model) for a utility session, or
    None when the turn must not run: master kill switch off, conversation
    missing (denied/deleted) or disabled."""
    uc_settings = getattr(ctx.settings, "utility_conversations", None)
    if uc_settings is not None and not uc_settings.enabled:
        return None
    from server.repositories.utility_conversations import (
        UtilityConversationRepository,
    )
    row = await UtilityConversationRepository(ctx.db).get(session_key)
    if not row or not row["enabled"]:
        return None
    from server.services import model_registry
    model = model_registry.resolve(row["model_alias"] or "cheap",
                                   ctx.settings.config_dir)
    charter_block = f"[Charter: {row['title']}]\n{row['charter']}"
    return charter_block, model


# ── spec validation + rendering ───────────────────────────────────────

def _validate_spec(*, name: str, source: str, type_pattern: str, level: str,
                   charter: str, hours: str, cooldown_s: int,
                   budget_per_hour: int) -> str | None:
    if not SLUG_RE.match(name):
        return ("name must be a lowercase slug (letters, digits, hyphens; "
                "2-41 chars), e.g. 'arrival-herald'")
    if not source or not re.match(r"^[a-z0-9-]+$", source):
        return "source must be a lowercase stimulus source slug, e.g. 'frigate'"
    if not type_pattern or not TYPE_PATTERN_RE.match(type_pattern):
        return ("type_pattern must be a lowercase glob over the event type, "
                "e.g. 'activity.person' or 'activity.person.jamie'")
    if level not in ("action", "info", "*"):
        return "level must be one of action, info, *"
    if not (32 <= len(charter.strip()) <= 4000):
        return "charter must be 32-4000 chars — the behavior spec the turn runs on"
    if hours and not HOURS_RE.match(hours.strip()):
        return "hours must look like 'HH:MM-HH:MM' (local house time) or be empty"
    if not (MIN_COOLDOWN_S <= cooldown_s <= 86_400):
        return f"cooldown_s must be >= {MIN_COOLDOWN_S} (a herald is not a heartbeat)"
    if not (1 <= budget_per_hour <= MAX_BUDGET_PER_HOUR):
        return f"budget_per_hour must be 1-{MAX_BUDGET_PER_HOUR}"
    return None


def render_spec(spec: dict[str, Any], *, verb: str = "New") -> str:
    """The spec as the approval DM renders it (proposal.summary)."""
    valves = [
        f"hours {spec['hours']}" if spec.get("hours") else "hours 24h",
        f"cooldown {spec['cooldown_s']}s",
        f"budget {spec['budget_per_hour']}/hour",
    ]
    return (
        f"{verb} sensation route: {spec['name']}\n"
        f"Route: {spec['source']} / {spec['type_pattern']} / {spec['level']}"
        f" → agent:{spec['name']}:utility\n"
        f"Valves: {' · '.join(valves)}\n"
        f"Model: {spec.get('model_alias', 'cheap')}\n"
        f"Requested by: {spec.get('requested_by', '?')}\n"
        f"Charter:\n{spec['charter']}"
    )


# ── approval follow-through (registered from approval_tools) ──────────

def register() -> None:
    from server.services.approval_tools import (
        register_on_approved, register_on_rejected,
    )
    register_on_approved(APPROVAL_TYPE, _on_approved)
    register_on_rejected(APPROVAL_TYPE, _on_rejected)


def _proposal_of(row: dict[str, Any]) -> dict[str, Any]:
    try:
        proposal = json.loads(row.get("proposal_data") or "{}")
        return proposal if isinstance(proposal, dict) else {}
    except (TypeError, ValueError):
        return {}


async def _notify_requester(ctx: AppContext, row: dict[str, Any],
                            text: str) -> None:
    requester = row.get("requested_by") or ""
    if not requester:
        return
    try:
        from server.services.wake_service import wake_conversation
        await wake_conversation(ctx, requester, text, call_category="wakeup")
    except Exception:
        logger.exception("sensation-route notify failed for %s", requester)


async def _on_approved(ctx: Any, row: dict[str, Any]) -> None:
    from server.repositories.stimulus import StimulusRepository
    proposal = _proposal_of(row)
    route_id = proposal.get("route_id")
    if not route_id:
        return
    await StimulusRepository(ctx.db).set_route_enabled(
        int(route_id), True,
        note=f"approved by {row.get('reviewed_by') or 'owner'} "
             f"({row.get('responded_at') or ''})".strip())
    await _notify_requester(
        ctx, row,
        f"Sensation route '{proposal.get('name')}' was approved and is now "
        f"live (route {route_id}).")
    logger.info("sensation route %s enabled (approval %s)", route_id, row.get("id"))


async def _on_rejected(ctx: Any, row: dict[str, Any]) -> None:
    from server.repositories.stimulus import StimulusRepository
    from server.repositories.utility_conversations import (
        UtilityConversationRepository,
    )
    proposal = _proposal_of(row)
    route_id = proposal.get("route_id")
    session_key = proposal.get("session_key") or ""
    s_repo = StimulusRepository(ctx.db)
    if route_id:
        # delete_route_if_inert: a live route was approved later — a stale or
        # duplicate rejection must not kill the behavior.
        await s_repo.delete_route_if_inert(int(route_id))
    if session_key and not await s_repo.has_live_route(session_key):
        await UtilityConversationRepository(ctx.db).delete(session_key)
    await _notify_requester(
        ctx, row,
        f"Sensation route '{proposal.get('name')}' was rejected — the request "
        f"and its rows were removed.")
    logger.info("sensation route request %s rejected (approval %s)",
                route_id, row.get("id"))


# ── the request itself ────────────────────────────────────────────────

async def _owner_origin(ctx: AppContext, fallback: str) -> str:
    """Approval DMs land in the owner's DM; fall back to the requester."""
    try:
        from server.services.steering import owner_dm_session_key
        return await owner_dm_session_key(ctx) or fallback
    except Exception:
        return fallback


async def _has_pending_approval(ctx: AppContext, name: str) -> bool:
    from server.repositories.approvals import ApprovalRepository
    rows = await ApprovalRepository(ctx.db).pending_of_type(
        APPROVAL_TYPE, entity_id=name)
    return bool(rows)


def _is_narrowing(old: dict[str, Any], new: dict[str, Any]) -> bool:
    """True when the ONLY changes tighten valves (autonomous, plan Part 3).
    Adding hours where none existed narrows; changing/removing them widens.
    Charter or pattern changes always re-approve — 'narrower wording' is not
    machine-judgeable."""
    if (old["source"], old["type_pattern"], old["level"]) != \
            (new["source"], new["type_pattern"], new["level"]):
        return False
    if (old.get("charter") or "").strip() != new["charter"].strip():
        return False
    old_hours = old.get("hours") or ""
    if new["hours"] and old_hours and new["hours"] != old_hours:
        return False  # a moved/shrunk window is not provably narrower
    if not new["hours"] and old_hours:
        return False  # removing hours widens to 24h
    old_cooldown = old.get("cooldown_s")
    if old_cooldown and new["cooldown_s"] < old_cooldown:
        return False
    old_budget = old.get("budget_per_hour")
    if old_budget and new["budget_per_hour"] > old_budget:
        return False
    return True


async def _submit_for_approval(ctx: AppContext, spec: dict[str, Any],
                               route_id: int, *, verb: str,
                               session_key: str) -> dict[str, Any]:
    from server.services.effects import emit_and_deliver
    from uuid import uuid4

    if await _has_pending_approval(ctx, spec["name"]):
        return {"ok": False,
                "error": f"an approval for '{spec['name']}' is already "
                         f"pending with the owner"}
    result = await emit_and_deliver(
        ctx, kind="approval_request",
        idempotency_key=f"approval_request:{uuid4()}",
        payload={"approval_type": APPROVAL_TYPE,
                 "entity_id": spec["name"],
                 "title": f"Sensation route: {spec['name']}",
                 "description": ("" if verb == "New" else
                                 f"{verb} of an existing route — the diff "
                                 f"below is the new full spec."),
                 "proposal": {"summary": render_spec(spec, verb=verb),
                              "route_id": route_id,
                              "session_key": session_key,
                              **{k: spec[k] for k in
                                 ("name", "source", "type_pattern", "level",
                                  "charter", "hours", "cooldown_s",
                                  "budget_per_hour", "requested_by")}},
                 "requested_by": spec["requested_by"],
                 "origin_conversation_id": await _owner_origin(
                     ctx, spec["requested_by"])})
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "approval emit failed")}
    return {"ok": True, "route_id": route_id, "status": "pending approval"}


def make_sensation_route_tools(ctx: AppContext, session_key: str) -> list:
    """request_sensation_route + list_sensation_routes (plan Part 3).
    Available wherever workspace tools are; creation is ALWAYS gated by the
    owner's approval DM, so an untrusted session can at worst cause one
    rejectable request (pending-deduped per name)."""

    @tool
    async def request_sensation_route(
        name: str,
        source: str,
        type_pattern: str,
        charter: str,
        level: str = "action",
        hours: str = "",
        cooldown_s: int = DEFAULT_COOLDOWN_S,
        budget_per_hour: int = DEFAULT_BUDGET_PER_HOUR,
        route_id: int = 0,
    ) -> str:
        """Create or edit a utility conversation + stimulus route (a
        self-serve "when X happens, do Y" behavior). The rows are written
        INERT (route disabled); the owner gets one approval DM with the full
        spec and approve makes it live; reject deletes it. The utility turn
        runs headless on the cheap model with the charter as its behaviour
        spec and the platform's tools.

        Widening changes (pattern, level, charter, looser/removed valves,
        re-enabling a disabled route) disable the route and re-approve —
        the behavior pauses until approved, deliberately. Pure valve
        tightening applies immediately. hours is local house time
        'HH:MM-HH:MM' (empty = 24h). Pass route_id to edit; 0 creates."""
        from server.repositories.conversations import ConversationRepository
        from server.repositories.stimulus import StimulusRepository
        from server.repositories.utility_conversations import (
            UtilityConversationRepository, utility_session_key,
        )

        spec = {"name": (name or "").strip().lower(),
                "source": (source or "").strip().lower(),
                "type_pattern": (type_pattern or "").strip(),
                "level": level or "action",
                "charter": (charter or "").strip(),
                "hours": (hours or "").strip(),
                "cooldown_s": int(cooldown_s),
                "budget_per_hour": int(budget_per_hour),
                "requested_by": session_key}
        error = _validate_spec(
            name=spec["name"], source=spec["source"],
            type_pattern=spec["type_pattern"], level=spec["level"],
            charter=spec["charter"], hours=spec["hours"],
            cooldown_s=spec["cooldown_s"],
            budget_per_hour=spec["budget_per_hour"])
        if error:
            return json.dumps({"ok": False, "error": error})

        target = utility_session_key(spec["name"])
        s_repo = StimulusRepository(ctx.db)
        u_repo = UtilityConversationRepository(ctx.db)

        if route_id:
            old = await s_repo.get_route(int(route_id))
            if not old:
                return json.dumps({"ok": False,
                                   "error": f"route {route_id} not found"})
            if old["target_session"] != target:
                return json.dumps({"ok": False, "error": "name doesn't match "
                                   "the route's utility session (create a new "
                                   "behavior instead)"})
            util_row = await u_repo.get(target)
            old_with_charter = {**old,
                                "charter": (util_row or {}).get("charter", "")}
            narrowing = _is_narrowing(old_with_charter, spec) and old["enabled"]
            await u_repo.upsert(session_key=target, title=spec["name"],
                                charter=spec["charter"],
                                created_by=old.get("created_by") or session_key)
            if narrowing:
                await s_repo.update_route(
                    int(route_id), hours=spec["hours"] or "",
                    cooldown_s=spec["cooldown_s"],
                    budget_per_hour=spec["budget_per_hour"])
                return json.dumps({"ok": True, "route_id": route_id,
                                   "status": "applied (valve tightening)"})
            # widening (or re-enable): apply the spec inert, then re-approve.
            await s_repo.update_route(
                int(route_id),
                source=spec["source"],
                type_pattern=spec["type_pattern"],
                level=spec["level"],
                enabled=False,
                hours=spec["hours"] or "",
                cooldown_s=spec["cooldown_s"],
                budget_per_hour=spec["budget_per_hour"],
                note=f"widening — awaiting approval ({session_key})")
            return json.dumps(await _submit_for_approval(
                ctx, spec, int(route_id), verb="Edit", session_key=target))

        # create
        await ConversationRepository(ctx.db).ensure(target, title=spec["name"])
        await u_repo.upsert(session_key=target, title=spec["name"],
                            charter=spec["charter"],
                            created_by=session_key)
        route_id_new = await s_repo.insert_route(
            source=spec["source"], type_pattern=spec["type_pattern"],
            level=spec["level"], target_session=target,
            hours=spec["hours"] or None, cooldown_s=spec["cooldown_s"],
            budget_per_hour=spec["budget_per_hour"],
            note=f"awaiting approval (requested by {session_key})",
            created_by=session_key)
        return json.dumps(await _submit_for_approval(
            ctx, spec, route_id_new, verb="New", session_key=target))

    @tool
    async def list_sensation_routes() -> str:
        """List utility-conversation behaviors and their stimulus routes:
        name, pattern, valves, enabled state, and any approval pending."""
        from server.repositories.approvals import ApprovalRepository
        from server.repositories.stimulus import StimulusRepository
        from server.repositories.utility_conversations import (
            UtilityConversationRepository,
        )

        utilities = await UtilityConversationRepository(ctx.db).list()
        routes = await StimulusRepository(ctx.db).routes(enabled_only=False)
        by_target: dict[str, list[dict[str, Any]]] = {}
        for r in routes:
            by_target.setdefault(r["target_session"] or "", []).append(r)
        pending = {p["entity_id"] for p in await ApprovalRepository(
            ctx.db).pending_of_type(APPROVAL_TYPE)}
        out = []
        for u in utilities:
            out.append({
                "name": u["title"],
                "session_key": u["session_key"],
                "enabled": bool(u["enabled"]),
                "charter_chars": len(u["charter"] or ""),
                "model_alias": u["model_alias"],
                "routes": [{
                    "route_id": r["id"],
                    "pattern": f"{r['source']} / {r['type_pattern']} / {r['level']}",
                    "enabled": bool(r["enabled"]),
                    "hours": r.get("hours"),
                    "cooldown_s": r.get("cooldown_s"),
                    "budget_per_hour": r.get("budget_per_hour"),
                } for r in by_target.get(u["session_key"], [])],
                "approval_pending": u["title"] in pending,
            })
        return json.dumps({"ok": True, "utilities": out})

    return [request_sensation_route, list_sensation_routes]
