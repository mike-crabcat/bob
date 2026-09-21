"""Goal execution loop (docs/goal-execution-plan.md).

The continuation contract: a goal room's turns self-schedule. Every turn
MAY declare a continuation (``goal_continue_now`` / ``goal_wait``); the
system guarantees a next wake regardless — the declaration is a hint over
a deterministic spine (D1):

  declared continue_now  -> immediate follow-on slot (capped, D7)
  declared wait(t)       -> slot at t (typed args, never prose)
  no declaration + pending tasks -> event-driven (task settles wake the
                                    room; nothing to schedule)
  no declaration + no tasks      -> dead-man heartbeat only

One pending ``goal_continue`` slot per goal (D2); any event wake that
dispatches into the room cancels the pending slot first (event beats
timer — begin_turn does it). Budgets are rounds spent per room turn;
exhaustion forces a terminal frame, never silence (D13). Zero-delta
turns select the stall frame (D6).

Runtime state lives in ``goals.loop_state_json`` (system-owned; the model
never writes it directly). Everything here is inert unless
BOB_GOAL_LOOP=on.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from server.context import AppContext
from server.services.tools import Tool, tool

logger = logging.getLogger(__name__)

CONTINUE_KIND = "goal_continue"
DEADMAN_KIND = "goal_deadman"

# Frames a wake/brief can carry (D6, D13).
FRAME_CARRY = "carry"          # the model's own declared continuation
FRAME_REVIEW = "review"        # progress landed; evaluate/branch/spawn
FRAME_STALL = "stall"          # zero-delta turn; prune/close/block
FRAME_TERMINAL = "terminal"    # budget exhausted; last guaranteed round
FRAME_DEADMAN = "deadman"      # liveness floor fired

MAX_WAIT_MINUTES = 7 * 1440    # a declared wait never exceeds a week
DECISION_DELAY_MINUTES = 5     # progress + nothing pending -> short review slot


def loop_enabled(ctx: AppContext) -> bool:
    settings = getattr(ctx.settings, "goal_loop", None)
    return bool(settings and settings.enabled)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ---------------------------------------------------------------------------
# Loop state (goals.loop_state_json — system-owned)
# ---------------------------------------------------------------------------

def state_of(goal: dict[str, Any]) -> dict[str, Any]:
    raw = goal.get("loop_state_json")
    if not raw:
        return {}
    try:
        state = json.loads(raw)
        return state if isinstance(state, dict) else {}
    except (TypeError, ValueError):
        return {}


def _default_state(ctx: AppContext, kind: str) -> dict[str, Any]:
    return {
        "budget_total": ctx.settings.goal_loop.budget_for(kind),
        "budget_spent": 0,
        "consecutive_continue": 0,
        "stall_streak": 0,
        "skip_streak": 0,
        "declaration": None,
        "last_frame": None,
    }


async def state_for(ctx: AppContext, goal: dict[str, Any]) -> dict[str, Any]:
    """Loop state, seeding + persisting the default for pre-loop goals."""
    state = state_of(goal)
    if state and "budget_total" in state:
        return state
    state = _default_state(ctx, goal.get("kind") or "task")
    from server.repositories.goals import GoalRepository
    await GoalRepository(ctx.db).set_loop_state(goal["id"], state)
    return state


async def _save_state(ctx: AppContext, goal_id: str, state: dict[str, Any]) -> None:
    from server.repositories.goals import GoalRepository
    await GoalRepository(ctx.db).set_loop_state(goal_id, state)


async def _fetch_goal(ctx: AppContext, goal_id: str) -> dict[str, Any] | None:
    from server.repositories.goals import GoalRepository
    return await GoalRepository(ctx.db).get(goal_id)


def _room_goal(ctx: AppContext, session_key: str) -> dict[str, Any] | None:
    from server.services.goal_rooms import _room_goal as _rg
    return _rg(ctx, session_key)


# ---------------------------------------------------------------------------
# The single continuation slot (D2)
# ---------------------------------------------------------------------------

async def cancel_slot(ctx: AppContext, goal_id: str) -> int:
    """Cancel the goal's pending goal_continue rows (event beats timer)."""
    from server.repositories.wakeups import WakeupRepository
    return await WakeupRepository(ctx.db).cancel_kind_for_goal(
        goal_id, CONTINUE_KIND)


async def schedule_slot(
    ctx: AppContext, goal: dict[str, Any], at: datetime,
    frame: str, note: str = "",
) -> None:
    """Single-slot upsert: replace any pending continuation with this one."""
    from server.repositories.wakeups import WakeupRepository
    await cancel_slot(ctx, goal["id"])
    await WakeupRepository(ctx.db).schedule(
        conversation_id=goal["conversation_id"],
        not_before=_iso(max(at, _now())),
        goal_id=goal["id"],
        kind=CONTINUE_KIND,
        payload={"frame": frame, "note": note[:400]})


async def pending_slot(ctx: AppContext, goal_id: str) -> dict[str, Any] | None:
    from server.repositories.wakeups import WakeupRepository
    return await WakeupRepository(ctx.db).pending_of_kind(goal_id, CONTINUE_KIND)


# ---------------------------------------------------------------------------
# Declarations (durable cell — crash-safe hint, D1)
# ---------------------------------------------------------------------------

async def declare(
    ctx: AppContext, goal_id: str, kind: str, *,
    at: datetime | None = None, note: str = "",
) -> dict[str, Any]:
    goal = await _fetch_goal(ctx, goal_id)
    if goal is None or goal.get("status") != "active":
        return {"ok": False, "error": "goal not active"}
    state = await state_for(ctx, goal)
    state["declaration"] = {
        "kind": kind, "at": _iso(at) if at else None,
        "note": note[:400], "declared_at": _iso(_now()),
    }
    await _save_state(ctx, goal_id, state)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Ensure / reconcile (deadman + opening slot)
# ---------------------------------------------------------------------------

async def ensure_loop(ctx: AppContext, goal: dict[str, Any]) -> None:
    """Seed loop state, the dead-man series, and the opening carry slot.
    Idempotent — safe on reconcile. Also re-stamps the room charter: goals
    created before the flip carry a loop-less charter, and the continuation
    contract is behaviour spec — the room's model must be able to read it."""
    settings = ctx.settings.goal_loop
    await state_for(ctx, goal)  # seeds budget
    try:
        from server.services.goal_rooms import ensure_room
        await ensure_room(
            ctx, goal_id=goal["id"], objective=goal["objective"],
            kind=goal.get("kind") or "task", deadline=goal.get("deadline"),
            origin_session=goal.get("origin_conversation_id") or "")
    except Exception:
        logger.warning("goal loop: charter refresh failed for %s",
                       goal["id"], exc_info=True)
    from server.repositories.wakeups import WakeupRepository
    # Phase 6a: a loop goal's legacy fixed check-in series is redundant —
    # the dead-man is the liveness floor now. Cancel any pre-flip leftovers
    # (the 2026-09-28 weekly checkin riding the merch pilot was one).
    await WakeupRepository(ctx.db).cancel_kind_for_goal(goal["id"], "goal_checkin")
    if not await WakeupRepository(ctx.db).has_scheduled_kind(
            goal["id"], DEADMAN_KIND):
        await WakeupRepository(ctx.db).schedule(
            conversation_id=goal["conversation_id"],
            not_before=_iso(_now() + timedelta(minutes=settings.deadman_minutes)),
            goal_id=goal["id"],
            recurrence=f"+{settings.deadman_minutes}m",
            kind=DEADMAN_KIND,
            payload={"note": "dead-man heartbeat"})
    if await pending_slot(ctx, goal["id"]) is None:
        await schedule_slot(
            ctx, goal, _now() + timedelta(minutes=settings.initial_delay_minutes),
            FRAME_CARRY,
            "opening round: read the charter, write the initial state "
            "block, open your first strategies and register their tasks")


async def reconcile(ctx: AppContext, *, limit: int = 50) -> int:
    """Flip-on migration: active room goals missing their deadman/loop
    state get ensured. Called from the heartbeat while the loop is on."""
    if not loop_enabled(ctx):
        return 0
    from server.repositories.goals import GoalRepository
    from server.repositories.wakeups import WakeupRepository
    wake_repo = WakeupRepository(ctx.db)
    ensured = 0
    for goal in await GoalRepository(ctx.db).list_active(limit=200):
        if not goal["conversation_id"].startswith("agent:goal-"):
            continue
        if await wake_repo.has_scheduled_kind(goal["id"], DEADMAN_KIND):
            continue
        await ensure_loop(ctx, goal)
        ensured += 1
        if ensured >= limit:
            break
    if ensured:
        logger.info("goal loop: ensured deadman/loop state for %d goal(s)", ensured)
    return ensured


# ---------------------------------------------------------------------------
# Turn lifecycle (wired into wake_service._generic_wake_dispatch)
# ---------------------------------------------------------------------------

async def _tasks_metrics(ctx: AppContext, goal: dict[str, Any],
                         since: str | None = None) -> dict[str, int]:
    from server.repositories.tasks import TaskRepository
    return await TaskRepository(ctx.db).counts_for_goal(
        goal["id"], goal["conversation_id"], since_iso=since)


async def begin_turn(ctx: AppContext, session_key: str) -> dict[str, Any] | None:
    """Arm a room turn: event-beats-timer slot cancellation + the
    before-snapshot the settle-path delta is computed against. Returns a
    token for end_turn, or None when this session is not an active loop
    room."""
    if not loop_enabled(ctx):
        return None
    goal = await _room_goal(ctx, session_key)
    if goal is None:
        return None
    # Event beats timer: whatever wakes the room now supersedes a pending
    # declared wait. (The slot that TRIGGERED this dispatch already fired —
    # cancellation only touches 'scheduled' rows, so it is a no-op there.)
    await cancel_slot(ctx, goal["id"])
    from server.services.goal_state_service import parse_strategy
    started_at = _iso(_now())
    return {
        "goal_id": goal["id"],
        "started_at": started_at,
        "goal_version": goal.get("version") or 0,
        "known_n": len(parse_strategy(goal).known),
        "tasks": await _tasks_metrics(ctx, goal),
    }


async def end_turn(ctx: AppContext, token: dict[str, Any], result_text: str) -> None:
    """The deterministic spine (D1): spend the round, resolve any
    declaration, and guarantee the next wake."""
    settings = ctx.settings.goal_loop
    goal = await _fetch_goal(ctx, token["goal_id"])
    if goal is None or goal.get("status") != "active":
        return
    state = await state_for(ctx, goal)

    now_tasks = await _tasks_metrics(ctx, goal, since=token["started_at"])
    from server.services.goal_state_service import parse_strategy
    version_bump = (goal.get("version") or 0) > token["goal_version"]
    evidence_grew = len(parse_strategy(goal).known) > token["known_n"]
    delta_score = (min(now_tasks["spawned"], 3) + min(now_tasks["settled"], 3)
                   + (1 if version_bump else 0) + (1 if evidence_grew else 0)
                   + (1 if result_text.strip() else 0))

    state["budget_spent"] = int(state.get("budget_spent", 0)) + 1
    declaration = state.get("declaration")
    state["declaration"] = None
    declared = declaration if isinstance(declaration, dict) else None

    if declared is None:
        state["skip_streak"] = int(state.get("skip_streak", 0)) + 1
        logger.info(
            "goal loop: continuation-skip (goal=%s, skip_streak=%d, "
            "open_tasks=%d, delta=%d) — spine covers",
            goal["id"], state["skip_streak"], now_tasks["open"], delta_score)
    else:
        state["skip_streak"] = 0

    budget_left = int(state.get("budget_total", 0)) - state["budget_spent"]
    frame, at, note = None, None, ""
    if budget_left <= 0:
        frame, at = FRAME_TERMINAL, _now() + timedelta(minutes=1)
        state["consecutive_continue"] = 0
        state["stall_streak"] = 0
    elif declared and declared.get("kind") == "continue_now":
        if int(state.get("consecutive_continue", 0)) >= settings.continue_cap:
            # Forced cooldown (D7): the chain is spinning.
            frame = FRAME_STALL
            at = _now() + timedelta(minutes=settings.stall_minutes)
            note = ("[cooldown] continue_now chain hit the cap — rest, then "
                    "make this round count")
            state["consecutive_continue"] = 0
        else:
            frame = FRAME_CARRY
            at = _now()
            note = str(declared.get("note") or "")
            state["consecutive_continue"] = int(
                state.get("consecutive_continue", 0)) + 1
        state["stall_streak"] = 0
    elif declared and declared.get("kind") == "wait":
        frame, note = FRAME_CARRY, str(declared.get("note") or "")
        at = _parse(declared.get("at") or "") or _now()
        state["consecutive_continue"] = 0
        state["stall_streak"] = 0
    elif now_tasks["open"] > 0:
        # Event-driven pendulum: settles will wake the room. Nothing to
        # schedule — the dead-man bounds any silence (D3).
        state["consecutive_continue"] = 0
        state["stall_streak"] = 0
    elif delta_score > 0:
        # Progress, nothing pending: one short review round to decide
        # (close / branch / ladder rung) instead of waiting on the deadman.
        frame = FRAME_REVIEW
        at = _now() + timedelta(minutes=DECISION_DELAY_MINUTES)
        note = "progress landed and no branches are pending — decide next"
        state["consecutive_continue"] = 0
        state["stall_streak"] = 0
    else:
        state["stall_streak"] = int(state.get("stall_streak", 0)) + 1
        frame = FRAME_STALL
        at = _now() + timedelta(minutes=settings.stall_minutes)
        note = "no change this round"

    if frame is not None and at is not None:
        await schedule_slot(ctx, goal, at, frame, note)
    state["last_frame"] = frame or state.get("last_frame")
    state["last_delta"] = {
        "spawned": now_tasks["spawned"], "settled": now_tasks["settled"],
        "version_bump": version_bump, "evidence_grew": evidence_grew,
        "frame": frame,
    }
    await _save_state(ctx, goal["id"], state)


# ---------------------------------------------------------------------------
# Fire handlers (wired into goal_service.fire_wakeup)
# ---------------------------------------------------------------------------

def frame_brief(frame: str, goal: dict[str, Any], note: str = "") -> str:
    """The wake brief per frame — the review discipline, delivered with the
    work instead of hoped for (D6)."""
    from server.services.goal_state_service import parse_strategy, render_strategy
    state_block = render_strategy(parse_strategy(goal)) or \
        "(no state recorded yet — write one with room_state)"
    budget = ""
    loop = state_of(goal)
    if loop:
        budget = (f"\nBudget: {loop.get('budget_spent', 0)}/"
                  f"{loop.get('budget_total', '?')} rounds spent.")
    head = {
        FRAME_CARRY: "## Goal round (your declared continuation)",
        FRAME_REVIEW: "## Goal round — evaluate and decide",
        FRAME_STALL: ("## Goal round — STALLED\nThis round must change "
                      "something: prune a strategy, close the goal with "
                      "evidence, or declare blocked to the origin. A no-op "
                      "round is a failure, not rest."),
        FRAME_TERMINAL: ("## FINAL ROUND — budget exhausted\nComplete with "
                         "findings so far, declare blocked to the origin, or "
                         "request renewal there (goal_refill, origin-only). "
                         "After this round the loop stands down."),
        FRAME_DEADMAN: ("## Goal dead-man check\nNo room turn for a full "
                        "interval. Pick the thread up: act on the state "
                        "block or say why you can't."),
    }.get(frame, "## Goal round")
    lines = [head]
    if note:
        lines.append(f"Note: {note}")
    lines += ["", f"Objective: {goal['objective']}"]
    # Progress notes (origin-side update_goal writes, scope changes): these
    # MUST reach the room — the 2026-09-21 pilot's scope update lived here
    # and the opening round never saw it.
    progress = (goal.get("progress") or "").strip()
    if progress:
        lines += ["", "Progress notes (from outside the room — honour these):",
                  progress[-800:]]
    lines += ["", "Current state block:", state_block, budget.strip()]
    lines.append(
        "\nEnd EVERY round with a continuation: goal_continue_now(reason) "
        "to chain, goal_wait(minutes=…, until=…, reason=…) to pause, or "
        "nothing when branches are pending (their results wake you). "
        "complete_goal when the objective is met WITH evidence.")
    return "\n".join(p for p in lines if p is not None)


async def handle_continue_wakeup(
    ctx: AppContext, goal: dict[str, Any], payload: dict[str, Any],
) -> str:
    frame = payload.get("frame") or FRAME_CARRY
    state = await state_for(ctx, goal)
    state["last_frame"] = frame
    await _save_state(ctx, goal["id"], state)
    return frame_brief(frame, goal, str(payload.get("note") or ""))


async def handle_deadman_wakeup(ctx: AppContext, goal: dict[str, Any]) -> str | None:
    """None = healthy (a room turn happened within the interval) — skip the
    wake, keep the series. The brief otherwise."""
    settings = ctx.settings.goal_loop
    cutoff = _iso(_now() - timedelta(minutes=settings.deadman_minutes))
    from server.repositories.history import HistoryRepository
    last = await HistoryRepository(ctx.db).last_message_at(
        goal["conversation_id"], role="assistant")
    if last is not None and last >= cutoff:
        logger.info("goal loop: deadman skip — room spoke within interval "
                    "(goal=%s)", goal["id"])
        return None
    return frame_brief(FRAME_DEADMAN, goal)


# ---------------------------------------------------------------------------
# Room tools — declarations, evidence, the strategies tree (D4/D5)
# ---------------------------------------------------------------------------

def make_loop_tools(ctx: AppContext, session_key: str) -> list:
    """Loop tools for a goal room. Room-only by construction (the goal is
    resolved from the session); returns [] for non-rooms."""
    import asyncio

    async def _goal() -> dict[str, Any] | None:
        return await _room_goal(ctx, session_key)

    async def _mutate_strategy(goal_id: str, mutate) -> dict[str, Any]:
        """Single-writer tree rewrite: read-modify-write through the
        version CAS (the room IS the single writer of the tree)."""
        from server.repositories.goals import GoalRepository
        repo = GoalRepository(ctx.db)
        goal = await repo.get(goal_id)
        if goal is None or goal["status"] != "active":
            return {"ok": False, "error": "goal not active"}
        from server.services.goal_state_service import parse_strategy, strategy_json_for
        state = parse_strategy(goal)
        # Normalise the tree to plain dicts: a raw assignment below stores
        # whatever shape this list holds, and mixed StrategyBranch/dict
        # entries would break later rounds' dict access.
        state.strategies = [
            b.model_dump() if hasattr(b, "model_dump") else dict(b)
            for b in (state.strategies or [])]
        err = mutate(state)
        if err:
            return {"ok": False, "error": err}
        ok = await repo.revise(
            goal_id, expected_version=goal.get("version") or 0,
            strategy_json=strategy_json_for(state))
        if not ok:
            return {"ok": False, "error":
                    "stale version — re-read the goal and retry once"}
        return {"ok": True}

    @tool
    async def goal_continue_now(reason: str = "") -> str:
        """Chain another goal round immediately after this one. Capped
        (default 3 consecutive); overuse triggers a forced cooldown."""
        goal = await _goal()
        if goal is None:
            return json.dumps({"ok": False, "error": "not a goal room"})
        result = await declare(ctx, goal["id"], "continue_now", note=reason)
        return json.dumps(result)

    @tool
    async def goal_wait(minutes: int = 0, until: str = "", reason: str = "") -> str:
        """Pause the goal loop for a typed interval: minutes (1..10080) or
        an ISO-8601 instant. Never prose — an unparseable value is
        rejected. Pending branches still wake you when they settle."""
        goal = await _goal()
        if goal is None:
            return json.dumps({"ok": False, "error": "not a goal room"})
        at: datetime | None = None
        if minutes:
            if not 1 <= int(minutes) <= MAX_WAIT_MINUTES:
                return json.dumps({"ok": False, "error":
                                   f"minutes must be 1..{MAX_WAIT_MINUTES}"})
            at = _now() + timedelta(minutes=int(minutes))
        elif until:
            at = _parse(until)
            if at is None or at <= _now():
                return json.dumps({"ok": False, "error":
                                   "until must be a future ISO-8601 instant"})
        else:
            return json.dumps({"ok": False, "error":
                               "provide minutes or until"})
        result = await declare(ctx, goal["id"], "wait", at=at, note=reason)
        return json.dumps(result)

    @tool
    async def goal_evidence(note: str) -> str:
        """Append one evidence line to the goal's known-list. Anyone may
        read the goal; evidence is append-only — this never rewrites the
        strategy tree."""
        goal = await _goal()
        if goal is None:
            return json.dumps({"ok": False, "error": "not a goal room"})
        from server.repositories.goals import GoalRepository
        ok = await GoalRepository(ctx.db).append_known_line(
            goal["id"], note[:600])
        return json.dumps({"ok": bool(ok)})

    @tool
    async def strategy_open(hypothesis: str, first_step: str = "") -> str:
        """Open a candidate strategy on the tree: what you'll try and how
        it will be tested. Multiple open strategies are the point — the
        tree records what was considered, not just what won."""
        goal = await _goal()
        if goal is None:
            return json.dumps({"ok": False, "error": "not a goal room"})

        def mutate(state):
            existing = getattr(state, "strategies", None) or []
            sid = f"s{len(existing) + 1}"
            existing.append({"id": sid, "hypothesis": hypothesis[:400],
                             "status": "candidate", "verdict": "",
                             "first_step": first_step[:300]})
            state.strategies = existing
        result = await _mutate_strategy(goal["id"], mutate)
        return json.dumps(result)

    @tool
    async def strategy_result(
        strategy_id: str, evidence: str, verdict: str,
    ) -> str:
        """Record a result against a strategy: the evidence observed and
        the verdict. Sets status to 'won' or 'pruned' from the verdict's
        first word; anything else marks it 'tested'. Also appends the
        evidence to the goal's known-list."""
        goal = await _goal()
        if goal is None:
            return json.dumps({"ok": False, "error": "not a goal room"})
        verdict_l = (verdict or "").strip().lower()
        status = ("won" if verdict_l.startswith("won") else
                  "pruned" if verdict_l.startswith("prun") else "tested")

        def mutate(state):
            existing = getattr(state, "strategies", None) or []
            for s in existing:
                if s.get("id") == strategy_id:
                    s["status"] = status
                    s["verdict"] = verdict[:400]
                    return None
            return f"strategy {strategy_id} not found"

        result = await _mutate_strategy(goal["id"], mutate)
        if result.get("ok"):
            from server.repositories.goals import GoalRepository
            await GoalRepository(ctx.db).append_known_line(
                goal["id"],
                f"[{strategy_id} {status}] {evidence[:400]}")
        return json.dumps(result)

    @tool
    async def strategy_prune(strategy_id: str, reason: str) -> str:
        """Abandon a strategy, recording why. Pruned branches stay on the
        tree — 'why we abandoned approach B' must stay answerable."""
        goal = await _goal()
        if goal is None:
            return json.dumps({"ok": False, "error": "not a goal room"})

        def mutate(state):
            existing = getattr(state, "strategies", None) or []
            for s in existing:
                if s.get("id") == strategy_id:
                    if s.get("status") == "won":
                        return "won strategies are not prunable"
                    s["status"] = "pruned"
                    s["verdict"] = reason[:400]
                    return None
            return f"strategy {strategy_id} not found"

        return json.dumps(await _mutate_strategy(goal["id"], mutate))

    return [goal_continue_now, goal_wait, goal_evidence,
            strategy_open, strategy_result, strategy_prune]


# ---------------------------------------------------------------------------
# Charter block (build_charter appends this when the loop is on)
# ---------------------------------------------------------------------------

_KIND_PLAYBOOKS = {
    "research": (
        "Research discipline: branch, test, prune. Open strategies with "
        "strategy_open BEFORE testing them; experiments are tasks "
        "(register_task) whose completion wakes this room with results; "
        "record every outcome with strategy_result — pruned approaches "
        "with reasons are as valuable as wins. Prefer evidence over "
        "narrative."),
    "build": (
        "Build discipline: nothing is 'done' until the artefact passes its "
        "declared proof — tests green on a branch, visual verification "
        "where the artefact is visual. Work happens in tasks. Merge/deploy "
        "is NOT yours: request it in the origin conversation (D14)."),
    "negotiate": (
        "Negotiation discipline: every confirmation you need is a TASK "
        "whose expected_completer is the channel conversation talking to "
        "that person; their reply settles the task and wakes this room. "
        "No reply by due → the backstop wakes you → follow the ladder "
        "(re-DM, then email, then call). Status updates don't belong in "
        "groups — but GROUP PUSHES (pitching an offer to a group) are a "
        "legitimate move you cannot make yourself: request them via "
        "send_report to the origin, which executes with approval."),
    "sales_target": (
        "Sales discipline — BIAS TO ACTION. Work branches in PARALLEL, "
        "not a patient ladder: (1) warm-lead conversion tasks (completer "
        "= the DM conversation with each contact); (2) group pushes — "
        "you cannot post to groups yourself; request each pitch via "
        "send_report to the origin naming the group and the offer, and "
        "track it as a task; (3) new products — generate candidate "
        "ideas, validate demand CHEAPLY (ask the people who'd buy, via "
        "DM tasks) before building anything; (4) catalogue expansion "
        "within existing permissions. Don't wait when a cheap parallel "
        "move exists; waiting is for replies, not for nerve. Revenue "
        "proof is the ledger, not a feeling."),
    "event_plan": (
        "Event discipline: confirmations and bookings are tasks with "
        "channel completers; the date hardens only from written "
        "confirmations. Ladder on silence; close with the settled "
        "arrangement as evidence."),
}


def charter_loop_block(kind: str) -> str:
    """The continuation contract + per-kind playbook, appended to the room
    charter while the loop is enabled."""
    playbook = _KIND_PLAYBOOKS.get(kind)
    lines = [
        "## Goal loop (continuation contract)",
        "This room runs itself. End EVERY round with exactly one choice:",
        "- goal_continue_now(reason) — chain the next round now (capped)",
        "- goal_wait(minutes=…, until=…, reason=…) — pause precisely",
        "- nothing — when branches (tasks) are pending, their results wake you",
        "- complete_goal / declaring blocked ends the series.",
        "Declarations are hints with a guaranteed floor: forgotten calls "
        "fall back to event wakes and a dead-man heartbeat — but rounds "
        "that end without one are counted.",
    ]
    if playbook:
        lines += ["", playbook]
    return "\n".join(lines)
