"""Goal execution loop (docs/goal-execution-plan.md) — the named invariants:

- D1 hint-over-spine: declarations schedule; missing ones fall back to
  event-driven (pending tasks) or the dead-man; skips are counted
- D2 single continuation slot per goal; event beats timer (any wake into
  the room cancels the pending slot)
- D3 dead-man fires only after a silent interval (recent turn → skip)
- D4 append-only evidence from anywhere (append_known_line bumps version,
  no CAS); strategies tree lives in the state block
- D6 zero-delta turn selects the stall frame; progress-with-nothing-pending
  gets a short review slot
- D7 continue_now chains are capped → forced cooldown
- D13 rounds budget: exhaustion forces the terminal frame

Loop off by default (kill switch): create_goal takes the legacy
check-in path.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from server.repositories.goals import GoalRepository
from server.services import goal_loop, goal_service

ORIGIN = "agent:main:whatsapp:group:120363422982048691"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


async def _make_goal(ctx, kind: str = "research") -> dict:
    return await goal_service.create_goal(
        ctx, conversation_id=ORIGIN, objective="prove the loop",
        kind=kind, strategy={"v": 2, "refs": {"entities": [], "claims": []}})


async def _slot(db, goal_id) -> dict | None:
    return await db.fetch_one(
        "SELECT * FROM wakeups WHERE goal_id = ? AND kind = 'goal_continue' "
        "AND status = 'scheduled'", (goal_id,))


async def _deadmen(db, goal_id) -> list:
    return await db.fetch_all(
        "SELECT * FROM wakeups WHERE goal_id = ? AND kind = 'goal_deadman' "
        "AND status = 'scheduled'", (goal_id,))


@pytest.fixture
def loop_on(ctx):
    ctx.settings.goal_loop.enabled = True
    yield
    ctx.settings.goal_loop.enabled = False


# ------------------------------------------------------------- loop off (D9)

async def test_loop_off_takes_checkin_path(ctx):
    goal = await _make_goal(ctx)
    slot = await _slot(ctx.db, goal["id"])
    assert slot is None, "no continuation slot unless the loop is on"
    checkins = await ctx.db.fetch_all(
        "SELECT * FROM wakeups WHERE goal_id = ? AND kind = 'goal_checkin'",
        (goal["id"],))
    assert checkins, "legacy check-in series still created with loop off"


# ------------------------------------------------------- ensure_loop (D1/D3)

async def test_ensure_loop_seeds_deadman_and_opening_slot(ctx, loop_on):
    goal = await _make_goal(ctx)
    deadmen = await _deadmen(ctx.db, goal["id"])
    assert len(deadmen) == 1
    assert deadmen[0]["recurrence"] == "+1440m"
    slot = await _slot(ctx.db, goal["id"])
    assert slot is not None and json.loads(slot["payload_json"])["frame"] == "carry"
    checkins = await ctx.db.fetch_all(
        "SELECT * FROM wakeups WHERE goal_id = ? AND kind = 'goal_checkin'",
        (goal["id"],))
    assert not checkins, "loop on: no fixed check-in series"
    state = goal_loop.state_of(
        await GoalRepository(ctx.db).get(goal["id"]))
    assert state["budget_total"] == 40  # research default
    charter_state = state
    assert charter_state["budget_spent"] == 0


async def test_reconcile_backfills_existing_goals(ctx, loop_on):
    """Goals created before the flip get their loop ensured by reconcile."""
    ctx.settings.goal_loop.enabled = False
    goal = await _make_goal(ctx)
    assert await _deadmen(ctx.db, goal["id"]) == []
    ctx.settings.goal_loop.enabled = True
    ensured = await goal_loop.reconcile(ctx)
    assert ensured == 1
    assert len(await _deadmen(ctx.db, goal["id"])) == 1


# ----------------------------------------------------------- slot D2 (event>timer)

async def test_slot_is_single_and_replaced(ctx, loop_on):
    goal = await _make_goal(ctx)
    await goal_loop.schedule_slot(ctx, goal, datetime.now(timezone.utc),
                                  goal_loop.FRAME_STALL, "one")
    await goal_loop.schedule_slot(
        ctx, goal, datetime.now(timezone.utc) + timedelta(minutes=10),
        goal_loop.FRAME_CARRY, "two")
    slots = await ctx.db.fetch_all(
        "SELECT * FROM wakeups WHERE goal_id = ? AND kind = 'goal_continue' "
        "AND status = 'scheduled'", (goal["id"],))
    assert len(slots) == 1
    assert json.loads(slots[0]["payload_json"])["note"] == "two"


async def test_event_beats_timer(ctx, loop_on):
    """A declared wait (slot armed) is superseded by any later wake into
    the room: the event consumes the pending slot."""
    goal = await _make_goal(ctx)
    at = datetime.now(timezone.utc) + timedelta(hours=2)
    await goal_loop.declare(ctx, goal["id"], "wait", at=at)
    await goal_loop.end_turn(ctx, _token(goal), "waiting")
    slot = await _slot(ctx.db, goal["id"])
    assert slot is not None, "the declared wait armed the slot"
    token = await goal_loop.begin_turn(ctx, goal["conversation_id"])
    assert token is not None
    assert await _slot(ctx.db, goal["id"]) is None, \
        "event wake consumed the pending slot"


# ------------------------------------------------------------ end_turn spine

def _token(goal) -> dict:
    return {"goal_id": goal["id"], "started_at": _iso(datetime.now(timezone.utc)),
            "goal_version": goal.get("version") or 0, "known_n": 0,
            "tasks": {"open": 0, "spawned": 0, "settled": 0}}


async def test_declared_wait_schedules_carry_slot(ctx, loop_on):
    goal = await _make_goal(ctx)
    at = datetime.now(timezone.utc) + timedelta(minutes=25)
    await goal_loop.declare(ctx, goal["id"], "wait", at=at, note="caffeine")
    await goal_loop.end_turn(ctx, _token(goal), "worked a bit")
    slot = await _slot(ctx.db, goal["id"])
    assert slot is not None
    payload = json.loads(slot["payload_json"])
    assert payload["frame"] == "carry" and payload["note"] == "caffeine"
    # one round spent
    state = goal_loop.state_of(await GoalRepository(ctx.db).get(goal["id"]))
    assert state["budget_spent"] == 1


async def test_continue_now_chains_then_cools_down(ctx, loop_on):
    goal = await _make_goal(ctx)
    token = _token(goal)
    cap = ctx.settings.goal_loop.continue_cap
    for _ in range(cap):
        await goal_loop.declare(ctx, goal["id"], "continue_now")
        await goal_loop.end_turn(ctx, token, "chain")
    slot = await _slot(ctx.db, goal["id"])
    assert json.loads(slot["payload_json"])["frame"] == "carry"
    # one more continue_now at the cap → forced cooldown (stall frame)
    await goal_loop.declare(ctx, goal["id"], "continue_now")
    await goal_loop.end_turn(ctx, token, "chain again")
    slot = await _slot(ctx.db, goal["id"])
    payload = json.loads(slot["payload_json"])
    assert payload["frame"] == "stall" and "cooldown" in payload["note"]


async def test_no_declaration_pending_tasks_event_driven(ctx, loop_on):
    goal = await _make_goal(ctx)
    from server.services import tasks as task_svc
    await task_svc.register_task(
        ctx, waiter_session=goal["conversation_id"], title="experiment 1",
        due_minutes=60, source_goal_id=goal["id"])
    token = await goal_loop.begin_turn(ctx, goal["conversation_id"])
    await goal_loop.end_turn(ctx, token, "")
    assert await _slot(ctx.db, goal["id"]) is None, \
        "pending tasks are the pendulum — no slot scheduled"
    state = goal_loop.state_of(await GoalRepository(ctx.db).get(goal["id"]))
    assert state["skip_streak"] == 1, "the skip is counted (metrics)"


async def test_zero_delta_selects_stall_frame(ctx, loop_on):
    goal = await _make_goal(ctx)
    await goal_loop.end_turn(ctx, _token(goal), "")
    slot = await _slot(ctx.db, goal["id"])
    assert json.loads(slot["payload_json"])["frame"] == "stall"


async def test_progress_nothing_pending_gets_review_slot(ctx, loop_on):
    goal = await _make_goal(ctx)
    token = _token(goal)
    # evidence arrived during the turn: known grew
    await GoalRepository(ctx.db).append_known_line(goal["id"], "found the bug")
    await goal_loop.end_turn(ctx, token, "logged evidence")
    slot = await _slot(ctx.db, goal["id"])
    assert json.loads(slot["payload_json"])["frame"] == "review"


async def test_budget_exhaustion_forces_terminal(ctx, loop_on):
    goal = await _make_goal(ctx)
    state = goal_loop.state_of(await GoalRepository(ctx.db).get(goal["id"]))
    state["budget_total"] = 1
    state["budget_spent"] = 0
    await ctx.db.execute(
        "UPDATE goals SET loop_state_json = ? WHERE id = ?",
        (json.dumps(state), goal["id"]))
    await goal_loop.end_turn(ctx, _token(goal), "last useful round")
    slot = await _slot(ctx.db, goal["id"])
    assert json.loads(slot["payload_json"])["frame"] == "terminal"


# ------------------------------------------------------------------ dead-man

async def test_deadman_skips_when_room_spoke(ctx, loop_on):
    goal = await _make_goal(ctx)
    from server.services.session_service import SessionService
    await SessionService(ctx).add_message(
        goal["conversation_id"], "assistant", "I am working on it")
    assert await goal_loop.handle_deadman_wakeup(ctx, goal) is None


async def test_deadman_wakes_when_silent(ctx, loop_on):
    goal = await _make_goal(ctx)
    # silence: an assistant row older than the interval
    old = _iso(datetime.now(timezone.utc) - timedelta(minutes=2000))
    await ctx.db.execute(
        "UPDATE messages SET created_at = ? WHERE conversation_id = ?",
        (old, goal["conversation_id"]))
    brief = await goal_loop.handle_deadman_wakeup(ctx, goal)
    assert brief is not None and "dead-man" in brief


# -------------------------------------------------------------------- frames

async def test_frame_brief_carries_state_and_contract(ctx, loop_on):
    goal = await _make_goal(ctx)
    await GoalRepository(ctx.db).append_known_line(goal["id"], "e1")
    goal = await GoalRepository(ctx.db).get(goal["id"])  # fresh state
    brief = goal_loop.frame_brief(goal_loop.FRAME_STALL, goal, "no change")
    assert "STALLED" in brief and "e1" in brief
    assert "goal_wait" in brief, "every brief ends with the contract"


# ------------------------------------------------- evidence + strategies (D4)

async def test_append_known_line_bumps_version_no_cas(ctx, loop_on):
    goal = await _make_goal(ctx)
    v0 = (await GoalRepository(ctx.db).get(goal["id"]))["version"]
    assert await GoalRepository(ctx.db).append_known_line(goal["id"], "obs")
    row = await GoalRepository(ctx.db).get(goal["id"])
    assert row["version"] == v0 + 1
    assert "obs" in json.loads(row["strategy_json"])["known"]


async def test_strategy_tools_manage_tree(ctx, loop_on):
    goal = await _make_goal(ctx)
    room = goal["conversation_id"]
    tools = {t.name: t for t in goal_loop.make_loop_tools(ctx, room)}
    assert "strategy_open" in tools

    r = json.loads(await tools["strategy_open"].handler(
        hypothesis="headless chrome on CDP", first_step="spawn a test render"))
    assert r["ok"]
    row = await GoalRepository(ctx.db).get(goal["id"])
    branches = json.loads(row["strategy_json"])["strategies"]
    assert branches[0]["id"] == "s1" and branches[0]["status"] == "candidate"

    r = json.loads(await tools["strategy_result"].handler(
        strategy_id="s1", evidence="rendered blank", verdict="pruned: no gpu"))
    assert r["ok"]
    row = await GoalRepository(ctx.db).get(goal["id"])
    branches = json.loads(row["strategy_json"])["strategies"]
    assert branches[0]["status"] == "pruned"
    known = json.loads(row["strategy_json"])["known"]
    assert any("s1 pruned" in k for k in known), \
        "strategy_result also files evidence"

    r = json.loads(await tools["strategy_prune"].handler(
        strategy_id="s1", reason="already pruned"))
    assert r["ok"]  # re-prune is idempotent

    # render includes the branch (the review turns see the tree)
    from server.services.goal_state_service import parse_strategy, render_strategy
    text = render_strategy(parse_strategy(row))
    assert "s1" in text and "pruned" in text


async def test_wait_validation_rejects_prose(ctx, loop_on):
    goal = await _make_goal(ctx)
    tools = {t.name: t for t in goal_loop.make_loop_tools(
        ctx, goal["conversation_id"])}
    r = json.loads(await tools["goal_wait"].handler(until="tomorrow morning"))
    assert not r["ok"]
    r = json.loads(await tools["goal_wait"].handler(minutes=99999))
    assert not r["ok"]
    r = json.loads(await tools["goal_wait"].handler(minutes=30))
    assert r["ok"]


async def test_tools_refuse_non_room_sessions(ctx, loop_on):
    tools = {t.name: t for t in goal_loop.make_loop_tools(ctx, ORIGIN)}
    assert tools, "tools are built (goal lookup fails per-call instead)"
    r = json.loads(await tools["goal_continue_now"].handler())
    assert not r["ok"] and "not a goal room" in r["error"]


async def test_progress_reaches_the_brief(ctx, loop_on):
    """Origin-side update_goal notes (scope changes) must be visible to the
    room: the 2026-09-21 pilot's scope update lived in progress and the
    opening round never saw it."""
    from server.repositories.goals import GoalRepository as GR
    goal = await _make_goal(ctx)
    ok = await GR(ctx.db).revise(
        goal["id"], expected_version=goal.get("version") or 0,
        progress="Scope: idea generation + validation is now a core branch.")
    assert ok
    goal = await GR(ctx.db).get(goal["id"])
    brief = goal_loop.frame_brief(goal_loop.FRAME_CARRY, goal)
    assert "idea generation" in brief, "progress renders in every brief"


async def test_update_goal_folds_progress_into_known(ctx, loop_on):
    """The goal_revise effect files progress text as durable evidence."""
    from server.services import effects as effects_svc
    from server.services.goal_tools import _register_goal_executors
    _register_goal_executors()
    from server.services import goal_service
    goal = await goal_service.create_goal(
        ctx, conversation_id=ORIGIN, objective="fold check", kind="task",
        strategy={"v": 2, "refs": {"entities": [], "claims": []}})
    from server.repositories.goals import GoalRepository as GR
    v = (await GR(ctx.db).get(goal["id"]))["version"]
    result = await effects_svc.emit_and_deliver(
        ctx, kind="goal_revise",
        idempotency_key=f"goal_revise:{goal['id']}:{v}",
        payload={"goal_id": goal["id"], "progress": "mine groups for ideas",
                 "expected_version": v})
    assert result["ok"]
    row = await GR(ctx.db).get(goal["id"])
    known = json.loads(row["strategy_json"]).get("known") or []
    assert any("mine groups for ideas" in k for k in known), \
        "scope notes become state-block evidence the room reads"


def test_sales_target_charter_is_aggressive():
    block = goal_loop.charter_loop_block("sales_target")
    assert "BIAS TO ACTION" in block and "PARALLEL" in block
    assert "send_report" in block, "group pushes route via the origin"
    negotiate = goal_loop.charter_loop_block("negotiate")
    assert "GROUP PUSHES" in negotiate, "negotiate knows the origin route too"



async def test_ensure_loop_cancels_legacy_checkin(ctx, loop_on):
    """Phase 6a: flipping a goal onto the loop retires its fixed check-in
    series (the dead-man is the liveness floor)."""
    ctx.settings.goal_loop.enabled = False
    goal = await _make_goal(ctx)  # created off -> legacy checkin exists
    checkins = await ctx.db.fetch_all(
        "SELECT * FROM wakeups WHERE goal_id = ? AND kind = 'goal_checkin' "
        "AND status = 'scheduled'", (goal["id"],))
    assert checkins
    ctx.settings.goal_loop.enabled = True
    await goal_loop.ensure_loop(ctx, goal)
    remaining = await ctx.db.fetch_all(
        "SELECT * FROM wakeups WHERE goal_id = ? AND kind = 'goal_checkin' "
        "AND status = 'scheduled'", (goal["id"],))
    assert not remaining, "ensure_loop retires the legacy series"
