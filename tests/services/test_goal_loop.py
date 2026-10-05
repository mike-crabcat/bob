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


def test_performance_charter_mandates_improvement():
    """The crypto-goal gap (Mike 2026-09-22): performance rooms were
    custodians — reconcile-and-report — with no mandate to improve the
    strategy. The playbook must require attribution review, the probe
    lane, and operator-owned rules."""
    block = goal_loop.charter_loop_block("performance")
    assert "IMPROVE THE STRATEGY" in block
    assert "attribution" in block, "review rounds run the attribution"
    assert "strategy.toml" in block and "OPERATOR-OWNED" in block
    assert "send_report" in block, "rule changes route via the origin"
    assert "diligence gate" in block, "the open-list standing rules apply"


async def test_goals_block_demands_writes(ctx):
    """The figurine-doc intake gap (Mike 2026-09-25): agreements known to a
    conversation but recorded only as memory never reach the room — the
    goals block must instruct every goal-visible conversation to write
    decisions back to the goal."""
    from server.context import AppContext  # noqa: F401
    from server.services.context_assembler import ContextAssembler
    goal = await _make_goal(ctx)
    block = await ContextAssembler(ctx).goals_block(ORIGIN)
    assert "MUST write it to the goal" in block, \
        "goal-visible conversations are told decisions go to update_goal"


def test_standing_rules_make_origin_a_participant():
    from server.services.goal_rooms import build_charter
    charter = build_charter(goal_id="g", objective="o", kind="event_plan",
                            deadline=None, origin="agent:x:y")
    assert "PARTICIPANT" in charter
    assert "origin-relayed" in charter


async def test_branch_scope_excludes_nonroom_waiter_noise(ctx, loop_on):
    """2026-09-25 mug-delivery leak: a non-room goal (kind event, works in
    its origin group) showed every task the GROUP awaits as its branches.
    The waiter clause must apply to room sessions only."""
    from server.repositories.tasks import TaskRepository
    goal = await _make_goal(ctx, kind="outreach")  # wrapper kind: no room, works in group
    group = goal["conversation_id"]
    from server.services import tasks as task_svc
    await task_svc.register_task(ctx, waiter_session=group,
                                 title="Mike mug delivery weekend",
                                 due_minutes=120)   # unrelated, no source_goal
    await task_svc.register_task(ctx, waiter_session=group,
                                 title="real branch",
                                 due_minutes=120, source_goal_id=goal["id"])
    repo = TaskRepository(ctx.db)
    titles = [t["title"] for t in await repo.list_for_goal(goal["id"], group)]
    assert titles == ["real branch"], \
        "group-awaited tasks are not a non-room goal's branches"
    counts = await repo.counts_for_goal(goal["id"], group)
    assert counts["open"] == 1


async def test_any_outcome_label_gets_the_room(ctx, loop_on):
    """2026-09-25 live miss: kind 'event' (one word off 'event_plan')
    silently cost a goal its room, loop and playbook. Since the profile
    split (Phase 1) the room routes off profile — any outcome label gets
    one, and the label is kept as written."""
    for label in ("event", "celebration"):
        goal = await _make_goal(ctx, kind=label)
        assert goal["kind"] == label
        assert goal["profile"] == "outcome"
        assert goal["conversation_id"].startswith("agent:goal-"), label


async def test_promise_labels_get_no_room(ctx, loop_on):
    goal = await _make_goal(ctx, kind="outreach")
    assert goal["profile"] == "promise"
    assert not goal["conversation_id"].startswith("agent:goal-")


async def test_tool_accepts_any_label(ctx):
    from server.services.goal_tools import goal_tool_handlers
    tools = {t.name: t for t in goal_tool_handlers(ctx, ORIGIN)}
    r = json.loads(await tools["create_goal"].handler(
        objective="x", kind="celebration"))
    assert r["ok"], r


def test_charter_keeps_perperson_work_out_of_groups():
    from server.services.goal_rooms import build_charter
    charter = build_charter(goal_id="g", objective="o", kind="event_plan",
                            deadline=None, origin="agent:main:whatsapp:group:x")
    assert "in THAT PERSON'S DM" in charter
    assert "never carries that process" in charter
    assert "POST it to the group" in charter, \
        "shared artefacts (reveals) are group content, not DM-only"


async def test_settle_disables_the_room_and_kills_backstops(ctx, loop_on):
    """2026-09-26 zombie room: a cancelled goal's room stayed enabled and a
    stale task_due backstop woke it at 05:52 — stale reports + group chases
    from a dead goal. Settle must disable the room AND cancel task_due
    backstops aimed at it (they are task-keyed; cancel_for_goal misses
    them)."""
    from server.repositories.utility_conversations import (
        UtilityConversationRepository,
    )
    from server.services import goal_service, tasks as task_svc
    goal = await _make_goal(ctx, kind="task")
    room = goal["conversation_id"]
    await task_svc.register_task(ctx, waiter_session=room, title="branch",
                                 due_minutes=600, source_goal_id=goal["id"])
    assert (await UtilityConversationRepository(ctx.db).get(room))["enabled"]

    ok = await goal_service.settle_goal(ctx, goal["id"], status="cancelled",
                                        result="done", note="test")
    assert ok
    row = await UtilityConversationRepository(ctx.db).get(room)
    assert not row["enabled"], "a settled goal's room must never run again"
    due = await ctx.db.fetch_all(
        "SELECT * FROM wakeups WHERE kind='task_due' AND conversation_id=? "
        "AND status='scheduled'", (room,))
    assert not due, "backstops into a disabled room die with the goal"


async def test_repoint_moves_task_and_backstop(ctx, loop_on):
    """The carry-over half the 2026-09-26 zombie missed: repointing a task's
    waiter must move its already-scheduled backstop too."""
    from server.services import tasks as task_svc
    goal = await _make_goal(ctx, kind="task")
    room = goal["conversation_id"]
    task = await task_svc.register_task(ctx, waiter_session=room,
                                        title="carry", due_minutes=600)
    other = "agent:goal-00000000-0000-0000-0000-000000000001:utility"
    assert await task_svc.repoint_task(ctx, task["id"], waiter_session=other)
    w = await ctx.db.fetch_one(
        "SELECT origin_conversation_id AS waiter_session FROM goals "
        "WHERE id=? AND kind='promise'", (task["id"],))
    assert w["waiter_session"] == other
    b = await ctx.db.fetch_one(
        "SELECT conversation_id FROM wakeups WHERE kind='task_due' "
        "AND status='scheduled' AND json_extract(payload_json,'$.task_id')=?",
        (task["id"],))
    assert b and b["conversation_id"] == other, \
        "the backstop follows the task, not the old waiter"


async def test_sensation_routes_off_by_default(ctx, loop_on):
    """2026-09-26 (Mike): rooms waking on memory removed for now — no route
    seeding, no claim events emitted, no subscription tools or charter
    rules. The wake tier needed supersessions that never fire; zero room
    deliveries ever happened. Reversible via BOB_GOAL_ROOM_SENSATIONS=on."""
    from server.services import goal_rooms
    goal = await _make_goal(ctx, kind="task")
    routes = await ctx.db.fetch_all(
        "SELECT * FROM stimulus_routes WHERE target_session=?",
        (goal["conversation_id"],))
    assert not routes, "no subscription routes are seeded while off"
    out = await goal_rooms.emit_claim_sensations(
        ctx, session_key=ORIGIN, turn_message_id="m", batch={
            "claims": [{"id": "c1", "subject_id": "person-x",
                        "claim_type_key": "preference"}]})
    assert out["emitted"] == 0, "no claim events emitted while off"
    names = {t.name for t in goal_rooms.make_goal_room_tools(
        ctx, goal["conversation_id"])}
    assert "subscribe" not in names and "list_subscriptions" not in names


async def test_task_prompts_command_immediate_fail_on_no_capability(ctx):
    """2026-09-26 AI-doom spam root cause: the steered task demanded a DM,
    the group turn had no DM tool, and the model reasoned 'I can't DM from
    here' three times without ever calling task_fail — an unresolvable
    obligation cycling until the iteration cap. Every layer that mentions
    task_fail must command the immediate call with a concrete shape."""
    from server.services import tasks as task_svc
    import inspect
    src = inspect.getsource(task_svc)
    assert "IF YOUR TOOLS CANNOT DO WHAT THIS PROMISE ASKS" in src
    assert "fail it immediately and move on" in src
    # the tool surface carries it too
    tools = {t.name: t for t in task_svc.promise_tool_handlers(ctx, "agent:x:y")}
    assert "CALL THIS IMMEDIATELY" in tools["task_fail"].description[:200]


async def test_room_to_group_delegation_refused(ctx, loop_on):
    """The bouncer (Mike 2026-09-26): a goal room delegating per-person
    work to a GROUP is refused at registration — no task, no backstop, no
    steer — with guidance to re-register against the person's DM. The
    AI-doom spam was exactly this delegation sailing through warn-only."""
    from server.services import tasks as task_svc
    goal = await _make_goal(ctx, kind="task")
    room = goal["conversation_id"]
    out = await task_svc.register_task(
        ctx, waiter_session=room, title="Chase Chris re figurine concept",
        instruction="chase Chris via DM",
        expected_completer="agent:main:whatsapp:group:120363422982048691",
        due_minutes=240)
    assert not out.get("ok", True) and "cannot DM individuals" in out["error"]
    rows = await ctx.db.fetch_all(
        "SELECT * FROM goals WHERE kind='promise' AND origin_conversation_id=?", (room,))
    assert not rows, "no task row is created for the refused delegation"
    b = await ctx.db.fetch_all(
        "SELECT * FROM wakeups WHERE kind='task_due' AND conversation_id=?", (room,))
    assert not b, "no backstop armed for the refused delegation"

    # The correct shape still works: DM completer, and the tool surfaces it
    out = await task_svc.register_task(
        ctx, waiter_session=room, title="Chase Chris",
        instruction="reply in Chris's DM thread",
        expected_completer="agent:main:whatsapp:dm:61424616977",
        due_minutes=60)
    assert out.get("ok", True) and out["id"]
    # non-room waiters delegating to groups is out of scope (unchanged)
    out = await task_svc.register_task(
        ctx, waiter_session=ORIGIN, title="group thing",
        expected_completer="agent:main:whatsapp:group:120363422982048691")
    assert out.get("ok", True)


async def test_goal_directory_created_at_room_birth(ctx, loop_on, tmp_path):
    """The goal directory (goals/<id8>/) exists before round one — created
    service-side so the convention has a target without model compliance."""
    import pathlib
    old = ctx.settings.harness.workspace_dir
    ctx.settings.harness.workspace_dir = tmp_path
    try:
        goal = await _make_goal(ctx, kind="task")
        d = tmp_path / "goals" / goal["id"][:8]
        assert d.is_dir(), "ensure_room creates the goal directory"
        from server.services.goal_rooms import build_charter
        charter = build_charter(goal_id=goal["id"], objective="o", kind="task",
                                deadline=None, origin="x")
        assert f"goals/{goal['id'][:8]}/" in charter
        assert "goal_artefact" in charter
    finally:
        ctx.settings.harness.workspace_dir = old


async def test_goal_artefact_tool_registers_idempotently(ctx, loop_on):
    """The artefact registry: append-once per path, updates the description
    on re-record, renders into every brief so the room can't lose its files."""
    goal = await _make_goal(ctx, kind="task")
    tools = {t.name: t for t in goal_loop.make_loop_tools(
        ctx, goal["conversation_id"])}
    r = json.loads(await tools["goal_artefact"].handler(
        path="goals/x/david-sax-v1.png", what="David Sax Machine v1"))
    assert r["ok"]
    r = json.loads(await tools["goal_artefact"].handler(
        path="goals/x/david-sax-v1.png", what="David Sax Machine v1 FINAL"))
    assert r["ok"]
    row = await GoalRepository(ctx.db).get(goal["id"])
    arts = json.loads(row["strategy_json"])["artefacts"]
    assert len(arts) == 1 and arts[0]["what"].endswith("FINAL"), \
        "re-recording updates, never duplicates"
    # convention nudge for stray paths
    r = json.loads(await tools["goal_artefact"].handler(
        path="generated-images/y.png", what="stray"))
    assert r["ok"] and "goals/" in r.get("note", "")
    from server.services.goal_state_service import parse_strategy, render_strategy
    text = render_strategy(parse_strategy(await GoalRepository(ctx.db).get(goal["id"])))
    assert "Artefact: goals/x/david-sax-v1.png" in text
    # non-room sessions get a toolset that resolves no goal
    stray = {t.name: t for t in goal_loop.make_loop_tools(ctx, ORIGIN)}
    r = json.loads(await stray["goal_artefact"].handler(path="a.png"))
    assert not r["ok"]


async def test_sleep_honeypot_short_ok_long_refused_with_doctrine(ctx):
    """2026-09-29 honeypot: the wait impulse gets a front door. Short sleeps
    run; long sleeps are refused with the full pattern (register the
    follow-through, run_bg_process wakes on completion, NO timer, end
    turn); goal rooms get the goal_wait pointer."""
    import asyncio as _aio
    from server.services.workspace_tools import make_workspace_tools, _inline_sleep_seconds
    tools = {t.name: t for t in make_workspace_tools(ctx, session_key="agent:main:whatsapp:dm:1")}
    r = await tools["sleep"].handler(seconds=2, reason="pacing")
    assert "slept 2s" in r
    r = await tools["sleep"].handler(seconds=540)
    assert "not slept" in r and "run_bg_process WAKES YOU" in r
    assert "do NOT" in r and "add_goal(profile='promise')" in r, "the register-follow-through step is taught"
    room_tools = {t.name: t for t in make_workspace_tools(
        ctx, session_key="agent:goal-x:utility")}
    r = await room_tools["sleep"].handler(seconds=60)
    assert "goal_wait" in r, "rooms are pointed at the declarative wait"


async def test_bash_funnel_redirects_long_sleeps(ctx):
    """The bash side is the funnel, not the wall: plain `sleep N>10` is
    refused with the pointer to the sleep tool / run_bg_process pattern."""
    from server.services.workspace_tools import make_workspace_tools, _inline_sleep_seconds
    assert _inline_sleep_seconds("sleep 540; echo done") == 540
    assert _inline_sleep_seconds("sleep 5 && curl x") == 5
    assert _inline_sleep_seconds("echo sleep 999") is None
    assert _inline_sleep_seconds("curl x; sleep 65") == 65
    tools = {t.name: t for t in make_workspace_tools(ctx)}
    r = await tools["bash"].handler("sleep 600; echo hi")
    assert "not run" in r and "run_bg_process" in r
    r = await tools["bash"].handler("sleep 5 && echo hi")
    assert "not run" not in r  # short sleeps pass through


async def test_goals_block_hands_goal_work_to_the_room(ctx, loop_on):
    """2026-09-29 drift: a group turn ran the goal's Blender pipeline
    in-conversation while the room sat idle. The goals block must teach the
    handoff — goal-scoped WORK goes to the room via a task, results and
    reveals stay in the conversation."""
    from server.services.context_assembler import ContextAssembler
    await _make_goal(ctx, kind="task")
    block = await ContextAssembler(ctx).goals_block(ORIGIN)
    assert "HANDED to the goal's room" in block
    assert "delegate_goal" in block, "goal work is delegated to the room"
    assert "does not run the goal's pipelines" in block


def test_image_outputs_ride_tool_result_not_user_block():
    """2026-09-30: probe-verified on OpenAI-direct AND OpenRouter/GLM —
    function_call_output.output accepts input_image parts. The injection
    must ride the tool result (foldable) instead of a synthetic user block
    (which nothing folded: goal turns hit 1.2M tokens, 89% base64)."""
    from server.services.openai_service import _tool_result_messages
    from server.services.tools import ImageInjection
    rows = _tool_result_messages(
        ImageInjection(text="Image loaded from x.png",
                       data_url="data:image/png;base64,AAA"),
        "call_1", video_supported=True)
    assert len(rows) == 1
    r = rows[0]
    assert r["type"] == "function_call_output" and r["call_id"] == "call_1"
    assert isinstance(r["output"], list)
    kinds = [p["type"] for p in r["output"]]
    assert kinds == ["input_text", "input_image"], kinds


def test_aged_image_outputs_fold_keep_three():
    """The '3 turns' rule: newest 3 image tool outputs stay inline, older
    ones elide to a byte-stable stub; idempotent; text part survives."""
    from server.services import tool_loop_folding as tlf
    def img_row(i):
        return {"type": "function_call_output", "call_id": f"c{i}",
                "output": [{"type": "input_text",
                            "text": f"Image loaded from figurine-{i}.png"},
                           {"type": "input_image",
                            "image_url": "data:image/png;base64," + "A"*100}]}
    msgs = [{"role": "user", "content": "go"}] + [img_row(i) for i in range(6)]
    n = tlf.fold_aged_image_outputs(msgs, keep_last=3)
    assert n == 3
    outs = [m for m in msgs if m.get("type") == "function_call_output"]
    def parts(m):
        return [p for p in m["output"] if isinstance(p, dict)]
    for m in outs[:3]:   # OLDEST three — elided
        assert not any(p["type"] == "input_image" for p in parts(m))
        assert any("elided" in p for p in m["output"] if isinstance(p, str)), \
            "stub + provenance text both survive"
    for m in outs[3:]:   # newest three — kept inline
        assert any(p["type"] == "input_image" for p in parts(m)), "newest kept"
        assert any("figurine" in p.get("text", "") for p in parts(m))
    assert tlf.fold_aged_image_outputs(msgs, keep_last=3) == 0  # idempotent
    # text-fold must not choke on list outputs
    assert tlf.fold_aged_tool_outputs(msgs) == 0


async def test_conversational_window_budget(ctx):
    """2026-09-30 bookkeeping budget: placeholders/relays must not consume
    the conversational window — the 'Re: OpenAI dot agents' miss had the
    morning's chat ~8 scrolls up for the human but 60+ rows deep for the
    model. Newest N CONVERSATIONAL rows kept; bookkeeping runs inside the
    span collapse to newest + a count marker."""
    from server.services.session_service import SessionService
    from server.services.prompt_assembler import (
        _conversational_window, _is_bookkeeping)
    key = "agent:main:whatsapp:dm:999"
    svc = SessionService(ctx)
    # 6 conversational; bookkeeping in realistic runs (the figurine-burst
    # shape: placeholder + relay + report back-to-back)
    for i in range(6):
        await svc.add_message(key, "user", f"human says {i}")
        await svc.add_message(key, "user", f"[bg turn x{i} detached: job]",
                              provenance="bg_placeholder")
        await svc.add_message(key, "user", f"## Task COMPLETED — thing {i}",
                              provenance="task_relay")
        await svc.add_message(key, "user", f"[Report from agent:goal-x] s{i}",
                              provenance="steer")
    out = await _conversational_window(key, ctx.db, max_history=6)
    conv = [r for r in out if not _is_bookkeeping(r)]
    assert len(conv) == 6, "all six conversational rows kept"
    texts = " || ".join(str(r.get("content")) for r in out)
    assert "human says 5" in texts and "human says 0" in texts
    assert texts.count("collapsed") >= 1, "bookkeeping runs collapsed"
    assert "[Report from agent:goal-x] s5" in texts, \
        "each run's newest member kept verbatim"
    # classifier spot-checks
    assert _is_bookkeeping({"provenance": "bg_placeholder", "role": "user",
                            "content": "x"})
    assert not _is_bookkeeping({"provenance": None, "role": "user",
                                "content": "hello"})
    assert not _is_bookkeeping({"provenance": None, "role": "assistant",
                                "content": "hi"})


async def test_conversational_window_group_no_keyerror(ctx):
    """2026-10-01 outage regression: the collapsed-bookkeeping marker is a
    role-"user" synthetic row without sender_id; on GROUP sessions the
    sender-name lookup read row["sender_id"] on it and every dispatch with a
    collapsed run crashed (Bob stopped replying in the pirate-radio group).
    The DM-keyed budget test above can't catch this — is_group never
    evaluates the lookup."""
    from server.services.session_service import SessionService
    from server.services.prompt_assembler import build_chat_messages
    key = "agent:main:whatsapp:group:999"
    svc = SessionService(ctx)
    for i in range(6):
        await svc.add_message(key, "user", f"human says {i}",
                              sender_id="contact-a")
        await svc.add_message(key, "user", f"[bg turn x{i} detached: job]",
                              provenance="bg_placeholder")
        await svc.add_message(key, "user", f"## Task COMPLETED — thing {i}",
                              provenance="task_relay")
        await svc.add_message(key, "user", f"[Report from agent:goal-x] s{i}",
                              provenance="steer")
    # must not raise
    messages = await build_chat_messages(
        "hello?", session_key=key, db=ctx.db, max_history=6)
    rendered = " || ".join(str(m.get("content")) for m in messages)
    assert "collapsed" in rendered, "bookkeeping runs still collapsed"


async def test_room_session_tools_scope_by_creator_principal(ctx, loop_on):
    """2026-10-01 census gap: the wake path passed make_session_tools with
    NO trust params, scoping every room to its own history — the census
    goal had to route group-history reads through Mike's DM because its own
    get_session_messages refused the group. Rooms now inherit their
    creator's principal (same rule as group-lookup): trusted creator reads
    foreign groups; untrusted/NULL creator stays own-session-only."""
    from server.services.session_tools import make_session_tools
    from server.services.wake_service import _session_tool_principal

    # trusted-creator room: unrestricted
    tools = {t.name: t.handler for t in make_session_tools(
        ctx, session_key="agent:goal-x:utility",
        is_trusted=True, contact_id="c1")}
    out = json.loads(await tools["get_session_messages"](
        session_key="agent:main:whatsapp:group:120363422982048691",
        limit=5))
    assert "error" not in out or "not accessible" not in str(out.get("error", ""))

    # untrusted room: only its own session
    tools2 = {t.name: t.handler for t in make_session_tools(
        ctx, session_key="agent:goal-y:utility",
        is_trusted=False, contact_id=None)}
    out2 = json.loads(await tools2["get_session_messages"](
        session_key="agent:main:whatsapp:group:120363422982048691",
        limit=5))
    assert "not accessible" in str(out2.get("error", "")), \
        "untrusted rooms still cannot read foreign groups"


async def test_goal_room_principal_helper(ctx, loop_on):
    """The wake-path helper resolves a goal room's creator principal —
    trusted Mike -> (True, mike_id); NULL creator -> (False, None).
    Group origins don't auto-derive a creator (DM-only rule), so the
    creator is pinned explicitly here as the group flow does."""
    from server.services.wake_service import _session_tool_principal
    from server.repositories.contacts import ContactRepository
    from server.repositories.goals import GoalRepository
    import uuid as _uuid
    from server.repositories.contacts import ContactRepository as _CR
    mike = await _CR(ctx.db).get_default()
    if mike is None:  # test DB has no seeded owner — make one
        await _CR(ctx.db).create(
            name="Mike Test", phone_number="+61456224867",
            is_trusted=1)
        await ctx.db.execute(
            "UPDATE contacts SET is_default=1 "
            "WHERE phone_number='+61456224867'")
        mike = await _CR(ctx.db).get_default()
    goal = await goal_service.create_goal(
        ctx, conversation_id=ORIGIN, objective="pinned owner",
        kind="task", creator_contact_id=mike["id"],
        strategy={"v": 2, "refs": {"entities": [], "claims": []}})
    trusted, contact = await _session_tool_principal(
        ctx, goal["conversation_id"])
    assert trusted and contact == mike["id"]
    # NULL creator (system/dream goal) -> untrusted defaults
    sys_goal = await GoalRepository(ctx.db).create(
        conversation_id=goal["conversation_id"],
        objective="system thing", goal_id=str(_uuid.uuid4()))
    await ctx.db.execute(
        "UPDATE goals SET creator_contact_id=NULL WHERE id=?",
        (sys_goal["id"],))
    await ctx.db.execute(
        "UPDATE goals SET status='active' WHERE id=?", (sys_goal["id"],))
    t2, c2 = await _session_tool_principal(
        ctx, f"agent:goal-{sys_goal['id']}:utility")
    assert (t2, c2) == (False, None)


async def test_group_create_only_tools_pin_owner(ctx):
    """2026-10-04 creation-gate widening: untrusted GROUP turns get
    create_goal + list_goals ONLY (structuring an ask ≠ exercising reach),
    creation refuses without a named owner (the room scopes to that
    creator), and mutating tools stay absent. The 2026-10-01 census
    needed an operator workaround for exactly this door."""
    from server.services.goal_tools import goal_tool_handlers
    tools = {t.name: t for t in goal_tool_handlers(
        ctx, "agent:main:whatsapp:group:120363422982048691",
        create_only=True)}
    assert set(tools) == {"create_goal", "list_goals"}
    # no owner -> refused with the ask-who guidance
    r = json.loads(await tools["create_goal"].handler(
        objective="census v2", kind="build"))
    assert not r["ok"] and "needs an owner" in r["error"]
    # trusted surface unchanged: full set
    full = {t.name for t in goal_tool_handlers(
        ctx, "agent:main:whatsapp:dm:61456224867")}
    assert {"update_goal", "complete_goal"} <= full
