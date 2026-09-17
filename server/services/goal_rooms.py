"""Goal rooms — every goal gets its own conversation (docs/goal-rooms-plan.md).

A goal room is the utility conversation that works ONE goal: the charter is
its behaviour spec, its history is the goal's working memory (the state block
the room writes via room_state replaces the reviser's blind JSON patching),
and its sensation routes are its attention. The goals table is demoted to a
lifecycle registry: the row owns lifecycle, the room owns deliberation, one
writer per kind.

This module owns: room creation (charter + seeded subscriptions + check-in
wakeup series), the claim-sensation emitter (corrections wake, adds digest),
the room tool surface (room_state / room_close / room_spawn / subscribe /
unsubscribe / list_subscriptions), check-in rendering, the hygiene sweep,
and the dream-plan seeding hook. Wiring lives elsewhere: goal_service.create
branches to ensure_room, fire_wakeup knows kind=goal_checkin, the claim
router skips room goals (sensations replace candidate matching + probe), and
wake_service injects the room tools.

Valve authority (plan D7): the room manages WHAT it listens to (patterns
within the source allowlist); the platform owns HOW MUCH (router-enforced
ceilings — MIN_COOLDOWN_S / MAX_BUDGET_PER_HOUR, re-exported from the
utility-conversation constants).

Self-echo (plan D6) is absolute: claims originating from a room's own
conversation never route back to it, goal-room sessions are excluded from
silent-turn extraction (the 2026-09-16 evidence: David's WFH schedule was
claim-written 4× in 4.5h from Bob's own roster narration), and the emitter
skips room-origin batches.
"""

from __future__ import annotations

import fnmatch
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from server.context import AppContext
from server.services.tools import tool

logger = logging.getLogger(__name__)

# Re-exported so rooms can never drift from the utility-conversation ceilings.
from server.services.utility_conversations import (  # noqa: F401  (re-export)
    MAX_BUDGET_PER_HOUR,
    MIN_COOLDOWN_S,
    TYPE_PATTERN_RE,
)

ROOM_PREFIX = "agent:goal-"
ROOM_SUFFIX = ":utility"

# Claim-sensation TTLs (plan D4): a correction older than this rides the next
# check-in instead of waking; info events live long enough to be digested.
_ACTION_TTL_S = 6 * 3600
_INFO_TTL_S = 7 * 24 * 3600
_DIGEST_MAX_EVENTS = 30


def is_room_session(session_key: str) -> bool:
    return session_key.startswith(ROOM_PREFIX) and session_key.endswith(ROOM_SUFFIX)


def rooms_enabled(ctx: AppContext) -> bool:
    settings = getattr(ctx.settings, "goal_rooms", None)
    return bool(settings and settings.enabled)


def kind_gets_room(ctx: AppContext, kind: str) -> bool:
    settings = getattr(ctx.settings, "goal_rooms", None)
    return bool(settings and settings.enabled and settings.kind_gets_room(kind))


def room_session_key(goal_id: str) -> str:
    return f"{ROOM_PREFIX}{goal_id}{ROOM_SUFFIX}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Room creation (plan D1, D8, D13)
# ---------------------------------------------------------------------------

_STANDING_RULES = """\
Standing rules:
- Maintain the state block: end any turn that learned or decided something
  with room_state(...) — plan, known, open_questions, next_actions (each
  next_action carries an ISO-with-offset due). Keep it honest; it is what
  your check-ins and the dashboard read.
- Subscriptions are your attention: list_subscriptions / subscribe /
  unsubscribe. At each check-in ask what you are NOT listening to that you
  should be, and prune what has been noise.
- Outreach: when connected you can DM a contact directly
  (send_whatsapp_to_contact; pass parent_goal_id = your goal so the reply
  rolls back to you). Use it for individual follow-ups — chase the person,
  never wake the origin group to run a private errand. Group broadcasts are
  NOT yours: they stay with the origin conversation and its approvals.
- Close only with evidence: room_close(evidence=...) citing the message or
  claim ids that prove the objective is met. Verify results before claiming
  success; report a stall honestly (send_report) instead of waiting in
  silence.
- Deliberate here; act through the platform's tools. Humans read your
  send_report, not this conversation."""


def build_charter(*, goal_id: str, objective: str, kind: str,
                  deadline: str | None, origin: str) -> str:
    dl = f"\nDeadline: {deadline}" if deadline else "\nDeadline: none set"
    return (
        f"[Goal {goal_id}]\n"
        f"You are the goal room working exactly one goal:\n"
        f"Objective: {objective}\n"
        f"Kind: {kind}{dl}\n"
        f"Origin conversation (where the goal was asked, and where your "
        f"reports go): {origin}\n\n"
        f"{_STANDING_RULES}"
    )


async def ensure_room(
    ctx: AppContext, *, goal_id: str, objective: str, kind: str,
    deadline: str | None, origin_session: str,
) -> dict[str, Any]:
    """Create the utility-conversation row + conversation + report_to for a
    goal. Idempotent per session key (charter refresh on re-run)."""
    from server.repositories.conversations import ConversationRepository
    from server.repositories.utility_conversations import (
        UtilityConversationRepository,
    )

    session_key = room_session_key(goal_id)
    await ConversationRepository(ctx.db).ensure(
        session_key, title=f"goal: {objective[:60]}")
    repo = UtilityConversationRepository(ctx.db)
    row = await repo.upsert(
        session_key=session_key,
        title=f"goal: {objective[:60]}",
        charter=build_charter(goal_id=goal_id, objective=objective, kind=kind,
                              deadline=deadline, origin=origin_session),
        created_by="goal_rooms",
    )
    # report_to = the origin (plan: humans get reports, rooms get work). For
    # a spawned child, origin IS the parent room — reports roll up (D10).
    if origin_session and origin_session != session_key:
        await repo.set_report_to(session_key, origin_session)
    return row


async def seed_subscriptions(
    ctx: AppContext, *, room_key: str, origin_session: str,
    strategy: dict[str, Any] | None = None,
) -> int:
    """Generous initial route set (plan D8): strategy refs.entities plus
    entities the origin conversation recently discussed (the entity-mention
    index), capped at max_routes_per_room. All memory-source, action-level,
    ceiling-valved; the room prunes and widens from here."""
    settings = ctx.settings.goal_rooms
    entity_ids: list[str] = []
    refs = ((strategy or {}).get("refs") or {})
    for eid in refs.get("entities") or []:
        if isinstance(eid, str) and eid and eid not in entity_ids:
            entity_ids.append(eid)
    if origin_session:
        from server.services.memory.claim_router import (
            mentioned_entities_for_conversation,
        )
        for eid in await mentioned_entities_for_conversation(
                ctx.db, origin_session, limit=settings.max_routes_per_room):
            if eid and eid not in entity_ids:
                entity_ids.append(eid)
    entity_ids = entity_ids[:settings.max_routes_per_room]

    from server.repositories.stimulus import StimulusRepository
    s_repo = StimulusRepository(ctx.db)
    existing = {r["type_pattern"] for r in await s_repo.routes_for_target(room_key)}
    created = 0
    for eid in entity_ids:
        pattern = f"claim.write.{eid}"
        if pattern in existing:  # re-adoption must not duplicate routes
            continue
        await s_repo.insert_route(
            source="memory", type_pattern=pattern, level="action",
            target_session=room_key,
            cooldown_s=MIN_COOLDOWN_S, budget_per_hour=MAX_BUDGET_PER_HOUR,
            note=f"goal-room seed ({room_key})",
            created_by="goal_rooms", enabled=True)
        created += 1
    if created:
        logger.info("goal room %s: seeded %d subscription route(s)", room_key, created)
    return created


async def schedule_checkin(ctx: AppContext, goal: dict[str, Any]) -> None:
    """The check-in wakeup series (plan D13): owned by the wakeups table,
    recurring, created with the room — no room exists without a next
    check-in. Cadence fixed per deadline presence (derive later)."""
    from server.repositories.wakeups import WakeupRepository

    settings = ctx.settings.goal_rooms
    minutes = (settings.checkin_minutes_deadline if goal.get("deadline")
               else settings.checkin_minutes_plain)
    await WakeupRepository(ctx.db).schedule(
        conversation_id=goal["conversation_id"],
        not_before=(datetime.now(timezone.utc)
                    + timedelta(minutes=minutes)).isoformat(),
        goal_id=goal["id"],
        recurrence=f"+{minutes}m",
        kind="goal_checkin",
        payload={"note": "goal room check-in"},
    )


# ---------------------------------------------------------------------------
# Claim sensations (plan D4, D5, D6)
# ---------------------------------------------------------------------------

async def emit_claim_sensations(
    ctx: AppContext, *, session_key: str, turn_message_id: str,
    batch: dict[str, Any],
) -> dict[str, Any]:
    """One stimulus event per entity touched by the turn's claim batch.

    Tiering: supersession/correction → level=action (wakes subscribed rooms
    immediately through the normal router); routine adds → level=info (never
    wakes; rides the subscribed room's next check-in digest). Coalesced per
    entity per turn via the dedup key, so replay/re-delivery is idempotent.

    Self-echo (D6): batches originating from a goal room are skipped whole —
    a room's own narration must never manufacture its wakeup.
    """
    out = {"emitted": 0, "action": 0, "info": 0, "skipped_room": False}
    if not rooms_enabled(ctx):
        return out
    if is_room_session(session_key):
        out["skipped_room"] = True
        return out

    claims = batch.get("claims") or []
    if not claims:
        return out

    # Entities corrected in this batch: subjects of rows the new claims
    # superseded (superseded_by stores the JSON list of replacing ids).
    from server.services.memory.claim_router import superseded_subjects
    old_subjects = await superseded_subjects(ctx.db, [c["id"] for c in claims])
    corrected: set[str] = set()
    for subj in old_subjects:
        corrected.add(subj)
    for c in claims:
        if c.get("subject_id") and c["subject_id"] in old_subjects:
            corrected.add(c["subject_id"])

    by_entity: dict[str, list[dict[str, Any]]] = {}
    for c in claims:
        for eid in (c.get("subject_id"), c.get("object_id")):
            if eid:
                by_entity.setdefault(eid, []).append(c)

    # No consumer exists for self/relationship claims (2026-09-17 audit:
    # 25% of the stream, zero possible subscribers — self material belongs
    # to the self-brief, not goal attention). Skipped whole at emission.
    _NOISE_PREFIXES = ("self-bob", "relationship-bob-")
    by_entity = {e: cs for e, cs in by_entity.items()
                 if not e.startswith(_NOISE_PREFIXES)}

    from server.repositories.stimulus import StimulusRepository
    s_repo = StimulusRepository(ctx.db)
    now = _now_iso()
    for eid, eclaims in by_entity.items():
        level = "action" if eid in corrected else "info"
        summary = "; ".join(
            f"{c['subject_id']} {c['claim_type_key']}"
            + (f" -> {c['object_id']}" if c.get("object_id")
               else f" = {str(c.get('value'))[:120]}")
            for c in eclaims[:5])
        _, inserted = await s_repo.insert_event(
            source="memory", type_=f"claim.write.{eid}", level=level,
            ts=now, dedup_key=f"goalroom:{turn_message_id}:{eid}",
            ttl_s=_ACTION_TTL_S if level == "action" else _INFO_TTL_S,
            target_hint=None, summary=summary,
            body={"claims": [{"id": c["id"], "type": c["claim_type_key"],
                              "subject": c["subject_id"]}
                             for c in eclaims],
                  "turn_message_id": turn_message_id,
                  "origin_session": session_key})
        if inserted:
            out["emitted"] += 1
            out[level] += 1
    if out["emitted"]:
        logger.info("goal rooms: %d claim sensation(s) from %s "
                    "(%d action, %d info)", out["emitted"], session_key,
                    out["action"], out["info"])
    return out


async def claims_activity_for(
    ctx: AppContext, room_key: str, *, since_iso: str,
) -> dict[str, Any]:
    """The check-in's complete claim-activity view (operator ask 2026-09-17:
    full recent list, not a filtered digest, for coverage):

    - ``lines``: EVERY claim event since the cutoff, one per line, marked
      ✓ (matches one of this room's subscriptions) or · (not followed) —
      the room sees both what it caught and what went past.
    - ``unfollowed``: entities with the most events in the window that no
      subscription covers — subscribe candidates, ranked.
    - ``quiet``: subscriptions with zero events in the window, with route
      age — prune candidates.
    """
    from collections import Counter
    from server.repositories.stimulus import StimulusRepository

    s_repo = StimulusRepository(ctx.db)
    routes = [r for r in await s_repo.routes_for_target(
        room_key, enabled_only=True) if r["source"] == "memory"]
    patterns = [r["type_pattern"] for r in routes]
    rows = await s_repo.recent_events(
        source="memory", since_iso=since_iso,
        limit=_DIGEST_MAX_EVENTS * 4)

    lines: list[str] = []
    unfollowed: Counter[str] = Counter()
    followed: set[str] = set()
    overflow = 0
    for r in rows or []:
        entity = r["type"].removeprefix("claim.write.")
        hit = any(fnmatch.fnmatchcase(r["type"], p) for p in patterns)
        if hit:
            followed.add(entity)
        else:
            unfollowed[entity] += 1
        if len(lines) < _DIGEST_MAX_EVENTS:
            lines.append(f"{'✓' if hit else '·'} [{r['ts'][5:16]}] {r['summary']}")
        else:
            overflow += 1
    if overflow:
        lines.append(f"(+{overflow} older event(s) in the window not shown)")

    quiet = []
    for r in routes:
        base = r["type_pattern"].removeprefix("claim.write.")
        if "*" in base or "?" in base or "[" in base:
            continue  # glob patterns match too broadly to call quiet
        if base not in followed:
            created = _parse_ts(r.get("created_at") or "")
            age_days = (max((datetime.now(timezone.utc) - created).days, 0)
                        if created else 0)
            quiet.append((base, age_days))
    return {"lines": lines, "unfollowed": unfollowed.most_common(8),
            "quiet": quiet}


def _parse_ts(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Scheduled scans (operator-directed; delivered by goal_service.fire_wakeup)
# ---------------------------------------------------------------------------

_SCAN_DEFAULT_CRON = "0 9 * * *"     # daily 09:00 local
_SCAN_DEFAULT_TZ = "Australia/Perth"


def scan_spec_of(goal: dict[str, Any]) -> dict[str, Any]:
    """The goal's ``scan`` block from its state (rooms own it via room_state —
    extra keys round-trip the strategy envelope): {"cron", "tz", "brief",
    "groups": [session keys]}. Empty when the room has no scan cadence."""
    from server.services.goal_state_service import parse_strategy
    extra = parse_strategy(goal).model_extra or {}
    spec = extra.get("scan")
    return spec if isinstance(spec, dict) else {}


async def schedule_scan(ctx: AppContext, goal: dict[str, Any]) -> None:
    """The scan wakeup series: a second named cadence on the room (the
    check-in's sibling). Recurrence is cron-with-tz so it stays at local 9am
    across DST; the series dies with the goal like every other wakeup."""
    from server.repositories.wakeups import WakeupRepository

    spec = scan_spec_of(goal) or {}
    # not_before=now: the series fires once immediately (a fresh scan spec
    # should produce a scan, not wait for tomorrow), then the cron recurrence
    # takes over — next_cron_occurrence computes the 9am slot from now.
    await WakeupRepository(ctx.db).schedule(
        conversation_id=goal["conversation_id"],
        not_before=_now_iso(),
        goal_id=goal["id"],
        recurrence=f"cron:{spec.get('cron') or _SCAN_DEFAULT_CRON}",
        tz=spec.get("tz") or _SCAN_DEFAULT_TZ,
        kind="goal_scan",
        payload={"note": "goal room scheduled scan"},
    )


async def render_scan(ctx: AppContext, goal: dict[str, Any]) -> str:
    spec = scan_spec_of(goal)
    groups = spec.get("groups") or []
    lines = [
        f"## Morning scan — {goal['objective']}",
        "",
        "Read the last ~24h of these conversations (read_group_history):",
    ]
    for g in groups:
        lines.append(f"- {g}")
    lines += [
        "",
        str(spec.get("brief") or
            "Note anything merch-relevant: designs people mention, in-jokes, "
            "running bits, upcoming events, quotable moments."),
        "",
        "Propose at most ONE new idea per scan: record it in your state with "
        "evidence (group + message time), then pitch it where it belongs — a "
        "clear individual target gets a DM (send_whatsapp_to_contact, "
        "parent_goal_id = your goal so the reply rolls back here); group "
        "pitches belong to the origin conversation, not to you. Do not "
        "re-propose an idea already recorded in `known`. If nothing new, say "
        "so and skip — a quiet scan is a fine outcome. End with room_state "
        "if anything changed.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Check-in rendering (plan D13; delivered by goal_service.fire_wakeup)
# ---------------------------------------------------------------------------

async def last_checkin_at(ctx: AppContext, goal_id: str) -> str:
    from server.repositories.wakeups import WakeupRepository
    last = await WakeupRepository(ctx.db).last_fired_of_kind(goal_id, "goal_checkin")
    return last or (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()


async def render_checkin(ctx: AppContext, goal: dict[str, Any]) -> str:
    from server.services.goal_state_service import parse_strategy, render_strategy

    state = parse_strategy(goal)
    since = await last_checkin_at(ctx, goal["id"])
    activity = await claims_activity_for(
        ctx, goal["conversation_id"], since_iso=since)
    lines = [
        f"## Goal room check-in — {goal['objective']}",
        "",
        "Pick up the thread. Current state block:",
        render_strategy(state) or "(no state recorded yet — write one with room_state)",
    ]
    if activity["lines"]:
        lines += [
            "",
            f"Claim activity since your last check-in (✓ = you follow the "
            f"entity, · = it went past unfollowed):",
            *activity["lines"],
        ]
    if activity["unfollowed"]:
        cand = ", ".join(f"{e} ({n})" for e, n in activity["unfollowed"])
        lines += [
            "",
            "Entities active in the window you do NOT follow: " + cand,
            "If any are goal-relevant, subscribe (mind the route cap); "
            "otherwise let them pass.",
        ]
    if activity["quiet"]:
        q = ", ".join(f"{e} ({d}d silent)" for e, d in activity["quiet"])
        lines += [
            "",
            "Subscriptions with no events in the window: " + q,
            "Prune what has been noise (unsubscribe); keep what is simply "
            "quiet.",
        ]
    lines.append("")
    lines.append(
        "Decide: act on a next_action (due ones first), update the state block, "
        "adjust your subscriptions, report something worth knowing to the origin, "
        "close the goal if the objective is met WITH evidence — or if you are "
        "genuinely blocked or stalled, say so via send_report now rather than "
        "waiting out the next check-in. A no-op check-in that changes nothing is "
        "a stall signal, not a success.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Room tool surface (injected by wake_service for goal-room sessions)
# ---------------------------------------------------------------------------

async def _room_goal(ctx: AppContext, session_key: str) -> dict[str, Any] | None:
    """The active goal whose room this session is (None for non-rooms)."""
    if not is_room_session(session_key):
        return None
    from server.repositories.goals import GoalRepository
    return await GoalRepository(ctx.db).active_goal_in_conversation(session_key)


def _validate_dues(state: dict[str, Any]) -> str | None:
    """Plan D14: every non-empty next_action due must parse as an ISO
    instant — reject prose/bare dates at the write, not downstream."""
    from server.services.goal_service import extract_due_instant
    for na in state.get("next_actions") or []:
        due = str(na.get("due") or "").strip()
        if due and extract_due_instant(due) is None:
            return (f"next_action due {due!r} is not an ISO-with-offset "
                    f"instant (e.g. 2026-09-20T09:00:00+08:00) — fix it and retry")
    return None


async def subscribe_room_to_entity(
    ctx: AppContext, room_key: str, entity_id: str, *, note: str = "",
) -> int | None:
    """Platform-side subscription (idempotent by pattern) for a room. Used by
    the outreach auto-subscribe: engagement must route back to the room by
    construction — a mid-conversation reply in the target DM becomes claims,
    claims become sensations, the room hears them without waiting for the
    outreach goal to settle. Deliberately NOT capped by the room-managed
    route cap: engagement routing is a correctness property, not attention
    curation the room prunes."""
    if not entity_id or not is_room_session(room_key):
        return None
    from server.repositories.stimulus import StimulusRepository
    s_repo = StimulusRepository(ctx.db)
    pattern = f"claim.write.{entity_id}"
    for r in await s_repo.routes_for_target(room_key):
        if r["type_pattern"] == pattern:
            if not r["enabled"]:
                await s_repo.set_route_enabled(r["id"], True)
            return r["id"]
    return await s_repo.insert_route(
        source="memory", type_pattern=pattern, level="action",
        target_session=room_key,
        cooldown_s=MIN_COOLDOWN_S, budget_per_hour=MAX_BUDGET_PER_HOUR,
        note=note or f"outreach auto-subscribe ({room_key})",
        created_by="goal_rooms", enabled=True)


def make_goal_room_tools(ctx: AppContext, session_key: str) -> list:
    """Room-scoped tools: no goal_id juggling — the session IS the room.
    Available only inside goal-room turns (wake_service gates injection)."""

    @tool
    async def room_state(state: str) -> str:
        """Replace THIS room's goal state block (write it at the end of any
        turn that learned or decided something). Provide the COMPLETE state:
        {"plan": str, "known": [str], "open_questions": [str],
         "next_actions": [{"action": str, "due": str}],
         "refs": {"entities": [str], "claims": [str]}}
        Every next_action due must be an ISO instant with offset."""
        from server.repositories.goals import GoalRepository

        goal = await _room_goal(ctx, session_key)
        if goal is None:
            return json.dumps({"ok": False, "error": "no active goal in this room"})
        try:
            raw = json.loads(state or "{}")
        except json.JSONDecodeError as exc:
            return json.dumps({"ok": False, "error": f"state is not valid JSON: {exc}"})
        if not isinstance(raw, dict):
            return json.dumps({"ok": False, "error": "state must be a JSON object"})
        from server.services.goal_tools import _validate_strategy_payload
        checked = _validate_strategy_payload(json.dumps(raw))
        if "error" in checked:
            return json.dumps({"ok": False, "error": checked["error"]})
        bad_due = _validate_dues(raw)
        if bad_due:
            return json.dumps({"ok": False, "error": bad_due})
        for _ in range(3):  # CAS with re-read
            ok = await GoalRepository(ctx.db).revise(
                goal["id"], expected_version=goal["version"],
                strategy_json=json.dumps(checked["state"], ensure_ascii=False))
            if ok:
                return json.dumps({"ok": True})
            goal = await GoalRepository(ctx.db).get(goal["id"])
            if goal is None or goal["status"] != "active":
                return json.dumps({"ok": False, "error": "goal no longer active"})
        return json.dumps({"ok": False, "error": "state write raced; retry"})

    @tool
    async def room_close(result: str, evidence: str = "") -> str:
        """Close THIS room's goal as completed. ``evidence`` is REQUIRED:
        the message ids, claim ids, or concrete results that prove the
        objective is met (the close door refuses to record an unevidenced
        success). If child goals are still open, close or cancel them
        first."""
        from server.repositories.goals import GoalRepository

        goal = await _room_goal(ctx, session_key)
        if goal is None:
            return json.dumps({"ok": False, "error": "no active goal in this room"})
        if not evidence.strip():
            return json.dumps({"ok": False,
                               "error": "close refused: evidence is required "
                                        "(message/claim ids or concrete results "
                                        "proving the objective is met)"})
        repo = GoalRepository(ctx.db)
        open_children = await repo.children_of(goal["id"], status="active")
        if open_children:
            return json.dumps({
                "ok": False,
                "error": "close refused: child goals still open — "
                         f"{[c['id'] for c in open_children]}. Close or cancel "
                         "them first (room_spawn goals roll up to you)."})
        from server.services.goal_service import settle_goal
        ok = await settle_goal(
            ctx, goal["id"], status="completed",
            result=f"{result}\n\nEvidence: {evidence}")
        return json.dumps({"ok": bool(ok), "goal_id": goal["id"]})

    @tool
    async def room_spawn(objective: str, kind: str = "task",
                         deadline: str = "", strategy: str = "") -> str:
        """Spawn a CHILD goal with its own room, reporting to this room.
        Only for sub-goals with their own stimulus surface (own
        subscriptions, own deadline, own multi-turn work) — ordinary steps
        belong in your state block's next_actions, not in a child room."""
        goal = await _room_goal(ctx, session_key)
        if goal is None:
            return json.dumps({"ok": False, "error": "no active goal in this room"})
        settings = ctx.settings.goal_rooms
        from server.repositories.goals import GoalRepository
        repo = GoalRepository(ctx.db)
        open_children = await repo.children_of(goal["id"], status="active")
        if len(open_children) >= settings.max_children_per_parent:
            return json.dumps({"ok": False,
                               "error": f"open-child cap reached "
                                        f"({settings.max_children_per_parent})"})
        # Depth: my own root-chain length must stay under the cap.
        root = await repo.root_of(goal["id"])
        depth = 1
        walker = root or goal
        while walker.get("parent_goal_id"):
            walker = await repo.get(walker["parent_goal_id"]) or walker
            depth += 1
        if depth + 1 > settings.max_goal_depth:
            return json.dumps({"ok": False,
                               "error": f"goal depth cap reached "
                                        f"({settings.max_goal_depth})"})
        strategy_payload: dict | None = None
        if strategy.strip():
            from server.services.goal_tools import _validate_strategy_payload
            checked = _validate_strategy_payload(strategy)
            if "error" in checked:
                return json.dumps({"ok": False, "error": checked["error"]})
            strategy_payload = checked["state"]
        from server.services import goal_service
        child = await goal_service.create_goal(
            ctx, conversation_id=session_key,
            objective=objective, kind=kind or "task",
            origin_conversation_id=session_key,
            deadline=deadline or None, strategy=strategy_payload,
            parent_goal_id=goal["id"])
        return json.dumps({"ok": True, "goal_id": child["id"],
                           "room": child["conversation_id"]})

    @tool
    async def subscribe(type_pattern: str, source: str = "memory",
                        level: str = "action") -> str:
        """Subscribe this room to a sensation pattern — your attention is
        yours to manage. ``type_pattern`` is a lowercase glob over the event
        type (e.g. claim.write.person-david-shedden). Allowed sources:
        memory. ``action`` subscriptions wake you immediately; info-level
        events ride your check-in digest regardless, so prefer action for
        corrections you must react to now."""
        if not is_room_session(session_key):
            return json.dumps({"ok": False, "error": "not a goal room"})
        settings = ctx.settings.goal_rooms
        allowed = {s.strip() for s in settings.subscribe_sources.split(",")}
        if source not in allowed:
            return json.dumps({"ok": False,
                               "error": f"source must be one of {sorted(allowed)}"})
        if not TYPE_PATTERN_RE.match(type_pattern or ""):
            return json.dumps({"ok": False,
                               "error": "type_pattern must be a lowercase glob "
                                        "over the event type"})
        if level not in ("action", "info", "*"):
            return json.dumps({"ok": False, "error": "level must be action, info, *"})
        from server.repositories.stimulus import StimulusRepository
        s_repo = StimulusRepository(ctx.db)
        existing = await s_repo.routes_for_target(session_key)
        if len(existing) >= settings.max_routes_per_room:
            return json.dumps({"ok": False,
                               "error": f"route cap reached "
                                        f"({settings.max_routes_per_room}); "
                                        "unsubscribe something first"})
        for r in existing:
            if (r["source"] == source and r["type_pattern"] == type_pattern
                    and r["level"] == level):
                if not r["enabled"]:
                    await s_repo.set_route_enabled(r["id"], True)
                    return json.dumps({"ok": True, "route_id": r["id"],
                                       "reenabled": True})
                return json.dumps({"ok": True, "route_id": r["id"],
                                   "duplicate": True})
        rid = await s_repo.insert_route(
            source=source, type_pattern=type_pattern, level=level,
            target_session=session_key,
            cooldown_s=MIN_COOLDOWN_S, budget_per_hour=MAX_BUDGET_PER_HOUR,
            note=f"room self-subscribe ({session_key})",
            created_by=session_key, enabled=True)
        return json.dumps({"ok": True, "route_id": rid})

    @tool
    async def unsubscribe(route_id: int) -> str:
        """Remove one of this room's subscription routes (see
        list_subscriptions for ids)."""
        from server.repositories.stimulus import StimulusRepository
        s_repo = StimulusRepository(ctx.db)
        route = await s_repo.get_route(int(route_id))
        if not route or route.get("target_session") != session_key:
            return json.dumps({"ok": False,
                               "error": "route not found for this room"})
        await s_repo.delete_route(int(route_id))
        return json.dumps({"ok": True})

    @tool
    async def list_subscriptions() -> str:
        """This room's sensation subscriptions: route ids, patterns, valves."""
        from server.repositories.stimulus import StimulusRepository
        routes = await StimulusRepository(ctx.db).routes_for_target(session_key)
        return json.dumps({"ok": True, "subscriptions": [{
            "route_id": r["id"], "source": r["source"],
            "pattern": r["type_pattern"], "level": r["level"],
            "enabled": bool(r["enabled"]),
            "cooldown_s": r.get("cooldown_s"),
            "budget_per_hour": r.get("budget_per_hour"),
        } for r in routes]})

    @tool
    async def read_group_history(session_key: str, limit: int = 40) -> str:
        """Read recent messages from a WhatsApp GROUP this platform is a
        member of (read-only — the morning-scan eyes). Pass the full group
        session key, e.g. agent:main:whatsapp:group:12036.... Raises nothing;
        wrong shapes return ok=false with the reason."""
        key = (session_key or "").strip()
        if not (key.startswith("agent:main:whatsapp:group:") and
                key.count(":") >= 5):
            return json.dumps({"ok": False,
                               "error": "session_key must be a WhatsApp "
                                        "group session key "
                                        "(agent:main:whatsapp:group:<id>)"})
        from server.services.session_service import SessionService
        messages = await SessionService(ctx).get_messages(key, limit=min(max(limit, 1), 80))
        from server.services.base import local_iso
        return json.dumps({"ok": True, "session_key": key, "messages": [
            {"role": m.role, "sender": m.sender_id, "content": m.content,
             "at": local_iso(m.created_at) if m.created_at else None}
            for m in (messages or [])
        ]})

    return [room_state, room_close, room_spawn, subscribe, unsubscribe,
            list_subscriptions, read_group_history]


def room_turn_tools(ctx: AppContext, session_key: str) -> list:
    """The full room-turn tool surface (the wake_service seam): room tools
    plus, when the WhatsApp bridge is connected, the DM outreach tool.

    Outreach scope is deliberate (2026-09-16, the room's own complaint): a
    room that must chase an individual should DM that individual directly —
    handing the errand to the origin GROUP re-creates the 2026-09-14 WFH
    failure (public status checks instead of authorised per-contact
    escalation). send_whatsapp_to_contact keeps its own guards (contact
    lookup, allow_inbound_dm, bridge connectivity) and mints an outreach
    child goal whose settle rolls back to this room when parent_goal_id is
    passed. Group-send tools are NOT included: broadcasts keep the origin
    routing and the group-send approval gate."""
    tools = list(make_goal_room_tools(ctx, session_key))
    bridge = getattr(ctx, "whatsapp_bridge", None)
    if bridge is not None and getattr(bridge, "connected", False):
        from server.services.whatsapp_outreach_tools import (
            make_whatsapp_outreach_tools,
        )
        tools.extend(make_whatsapp_outreach_tools(ctx, bridge, session_key))
    return tools


# ---------------------------------------------------------------------------
# Hygiene sweep (route pruning; plan D11 backstop)
# ---------------------------------------------------------------------------

async def prune_orphan_routes(ctx: AppContext) -> int:
    """Delete enabled routes whose target room has no active goal. Direct
    settle already prunes; this is the backstop for rows that slipped past
    (crashed settles, manual SQL, adopted-then-cancelled goals)."""
    if not rooms_enabled(ctx):
        return 0
    from server.repositories.goals import GoalRepository
    from server.repositories.stimulus import StimulusRepository

    s_repo = StimulusRepository(ctx.db)
    g_repo = GoalRepository(ctx.db)
    pruned = 0
    for r in await s_repo.enabled_goal_room_routes():
        target = r["target_session"]
        if await g_repo.active_goal_in_conversation(target) is None:
            await s_repo.delete_route(int(r["id"]))
            pruned += 1
    return pruned


async def prune_room_routes(ctx: AppContext, goal_id: str) -> int:
    """Delete every route targeting this goal's room (settle path)."""
    from server.repositories.stimulus import StimulusRepository
    return await StimulusRepository(ctx.db).delete_routes_for_target(
        room_session_key(goal_id))


# ---------------------------------------------------------------------------
# Legacy-goal adoption (rollout: room an existing active goal)
# ---------------------------------------------------------------------------

async def adopt_goal(ctx: AppContext, goal_id: str) -> dict[str, Any]:
    """Give an existing ACTIVE goal a room: create the room, retarget the
    goal's working conversation, seed subscriptions + check-ins. Wrapper
    kinds and non-active goals are refused. Idempotent per goal (re-adopt
    refreshes the charter)."""
    from server.repositories.goals import GoalRepository

    goal = await GoalRepository(ctx.db).get(goal_id)
    if goal is None:
        return {"ok": False, "error": "goal not found"}
    if goal["status"] != "active":
        return {"ok": False, "error": f"goal is {goal['status']}, not active"}
    if not kind_gets_room(ctx, goal["kind"]):
        return {"ok": False,
                "error": f"kind {goal['kind']!r} does not get a room"}
    if not rooms_enabled(ctx):
        return {"ok": False, "error": "goal rooms are disabled"}

    origin = goal.get("origin_conversation_id") or goal["conversation_id"]
    if is_room_session(goal["conversation_id"]):
        room_key = goal["conversation_id"]
    else:
        room_key = room_session_key(goal_id)
    await ensure_room(ctx, goal_id=goal_id, objective=goal["objective"],
                      kind=goal["kind"], deadline=goal.get("deadline"),
                      origin_session=origin)
    if goal["conversation_id"] != room_key:
        repo = GoalRepository(ctx.db)
        await repo.set_conversation(goal_id, room_key)
        await repo.add_holder(goal_id, room_key, role="worker")
        if origin and origin != room_key:
            await repo.add_holder(goal_id, origin, role="origin")

    from server.services.goal_state_service import parse_strategy
    state = parse_strategy(goal)
    strategy = {"refs": {"entities": state.refs.entities,
                         "claims": state.refs.claims}}
    await seed_subscriptions(ctx, room_key=room_key, origin_session=origin,
                             strategy=strategy)
    goal = await GoalRepository(ctx.db).get(goal_id)
    from server.repositories.wakeups import WakeupRepository
    if not await WakeupRepository(ctx.db).has_scheduled_of_kind(
            goal_id, "goal_checkin") and goal is not None:
        await schedule_checkin(ctx, goal)
    logger.info("goal %s adopted into room %s", goal_id, room_key)
    return {"ok": True, "room": room_key}


# ---------------------------------------------------------------------------
# Dream-plan seeding (plan D15 — dream_plans.task_id finally gets a writer)
# ---------------------------------------------------------------------------

async def seed_room_for_plan(ctx: AppContext, plan: dict[str, Any]) -> dict[str, Any]:
    """Approved dream plan → goal room. Charter from the plan's proposed
    action + assistance method; the plan's linked session becomes the
    origin. Returns the room result; never raises into the caller."""
    try:
        if not rooms_enabled(ctx):
            return {"ok": False, "error": "disabled"}
        from server.services import goal_service
        from server.services.dream.store import DreamStore
        # The plan's evidence session is the origin — the room's reports go
        # where the commitment was detected (dream links carry session_key).
        session_key = (await DreamStore(ctx).link_session_for_item(
            "plan", plan["id"])) or ""
        goal = await goal_service.create_goal(
            ctx, conversation_id=session_key or "agent:main:internal",
            objective=f"[dream plan {plan['id']}] {plan['title']}",
            kind="task",
            origin_conversation_id=session_key or None,
            strategy={"v": 2, "plan": str(plan.get("proposed_action") or ""),
                      "known": [f"assistance: {plan.get('assistance_method') or ''}"],
                      "open_questions": [], "next_actions": [],
                      "refs": {"entities": [], "claims": []}},
        )
        await DreamStore(ctx).set_plan_task_id(plan["id"], goal["id"])
        return {"ok": True, "goal_id": goal["id"],
                "room": goal["conversation_id"]}
    except Exception:
        logger.exception("dream plan %s: goal-room seeding failed",
                         plan.get("id"))
        return {"ok": False, "error": "seeding failed (logged)"}
