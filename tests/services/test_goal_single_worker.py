"""2026-10-09 Night Watch fixes: one worker per goal (the opening round
waits for the creating turn), self-closures don't wake their closer, and
'superseded' is not a failure."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from server.repositories.goals import GoalRepository
from server.repositories.wakeups import WakeupRepository
from server.services import goal_loop, goal_service

ORIGIN = "agent:main:whatsapp:group:120363408889690088"


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


async def _live_turn(ctx, conversation_id):
    exp = _iso(datetime.now(timezone.utc) + timedelta(minutes=5))
    await ctx.db.execute(
        "INSERT INTO turns (id, conversation_id, status, lease_owner, "
        "lease_expires_at, attempt, started_at, created_at) "
        "VALUES ('t-live', ?, 'running', 'test', ?, 1, ?, ?)",
        (conversation_id, exp, _iso(datetime.now(timezone.utc)),
         _iso(datetime.now(timezone.utc))))


async def _goal(ctx):
    return await goal_service.create_goal(
        ctx, conversation_id=ORIGIN, objective="campaign: set + poster + ads",
        kind="event_plan")


async def test_opening_round_defers_while_origin_turn_live(ctx, monkeypatch):
    monkeypatch.setattr(ctx.settings.goal_loop, "enabled", True)
    goal = await _goal(ctx)
    from server.repositories.conversations import ConversationRepository
    origin_cid = await ConversationRepository(ctx.db).resolve_cid(ORIGIN)
    await _live_turn(ctx, origin_cid)
    goal = await GoalRepository(ctx.db).get(goal["id"])
    deferred = await goal_loop.defer_opening_round(ctx, goal, {"frame": "carry"})
    assert deferred is True
    slot = await WakeupRepository(ctx.db).pending_of_kind(goal["id"], "goal_continue")
    assert slot is not None
    when = datetime.fromisoformat(slot["not_before"].replace("Z", "+00:00"))
    assert when > datetime.now(timezone.utc) + timedelta(seconds=30)


async def test_opening_round_runs_when_origin_idle(ctx, monkeypatch):
    monkeypatch.setattr(ctx.settings.goal_loop, "enabled", True)
    goal = await GoalRepository(ctx.db).get((await _goal(ctx))["id"])
    assert await goal_loop.defer_opening_round(ctx, goal, {"frame": "carry"}) is False


async def test_later_rounds_never_defer(ctx, monkeypatch):
    monkeypatch.setattr(ctx.settings.goal_loop, "enabled", True)
    goal = await _goal(ctx)
    from server.repositories.conversations import ConversationRepository
    await _live_turn(ctx, await ConversationRepository(ctx.db).resolve_cid(ORIGIN))
    await GoalRepository(ctx.db).set_loop_state(goal["id"], {"budget_spent": 3})
    goal = await GoalRepository(ctx.db).get(goal["id"])
    assert await goal_loop.defer_opening_round(ctx, goal, {"frame": "carry"}) is False


async def test_origin_closing_its_own_goal_is_not_woken(ctx):
    goal = await _goal(ctx)
    with patch("server.services.wake_service.wake_conversation",
               new=AsyncMock()) as wake:
        ok = await goal_service.settle_goal(
            ctx, goal["id"], status="cancelled", result="superseded: x",
            closer_session=ORIGIN)
    assert ok
    wake.assert_not_called()


async def test_other_closer_still_wakes_origin(ctx):
    goal = await _goal(ctx)
    with patch("server.services.wake_service.wake_conversation",
               new=AsyncMock()) as wake:
        await goal_service.settle_goal(
            ctx, goal["id"], status="completed", result="done",
            closer_session=goal["conversation_id"])
    assert wake.await_count == 1


async def test_self_settled_promise_does_not_wake_its_waiter(ctx):
    from server.services import tasks as task_svc
    with patch("server.services.wake_service.wake_conversation",
               new=AsyncMock()) as wake:
        t = await task_svc.register_task(ctx, waiter_session=ORIGIN,
                                         title="placeholder")
        tid = t.get("task_id") or t.get("id") or t.get("goal_id")
        await task_svc.settle_task(ctx, tid, to_status="cancelled",
                                   result="oops", completed_by=f"conversation:{ORIGIN}")
        await task_svc.deliver_settlement(ctx, tid)
    wake.assert_not_called()
    from server.repositories.tasks import TaskRepository
    assert (await TaskRepository(ctx.db).get(tid))["delivered_at"]


async def test_state_write_rewrites_progress_from_plan(ctx):
    goal = await _goal(ctx)
    repo = GoalRepository(ctx.db)
    await repo.revise(goal["id"], expected_version=goal["version"],
                      progress="not yet booked")
    g = await repo.get(goal["id"])
    await repo.revise(g["id"], expected_version=g["version"],
                      strategy_json=json.dumps({"v": 2, "plan": "READY + SCHEDULED 21:00"}))
    assert (await repo.get(goal["id"]))["progress"] == "READY + SCHEDULED 21:00"


async def test_routine_written_in_a_room_is_listed_on_the_goal(ctx):
    from server.services.routine_tools import make_routine_tools
    goal = await _goal(ctx)
    room = goal["conversation_id"]
    write = next(t for t in make_routine_tools(ctx, session_key=room)
                 if t.name == "write_routine")
    out = await write.handler(routine_yaml=(
        "name: hourly-ads\nschedule: '45 12-20 * * *'\n"
        "prompt: Queue the promo with radio.py feature promote.\n"))
    assert "goal link" in out
    state = json.loads((await GoalRepository(ctx.db).get(goal["id"]))["strategy_json"])
    assert any("[mechanism] routine hourly-ads" in k for k in state["known"])


async def test_explicit_goal_link_from_origin(ctx):
    from server.services.goal_links import link_mechanism
    goal = await _goal(ctx)
    note = await link_mechanism(ctx, ORIGIN, goal["id"], "bg job ads (job 1): promo loop")
    assert "linked" in note
    assert await link_mechanism(ctx, ORIGIN, "", "x") == ""


async def test_subagent_settled_promise_wakes_human_chat_as_relay(ctx):
    from server.services import tasks as task_svc
    with patch("server.services.wake_service.wake_conversation",
               new=AsyncMock()) as wake:
        t = await task_svc.register_task(ctx, waiter_session=ORIGIN,
                                         title="subagent claude: build share skill")
        tid = t.get("task_id") or t.get("id") or t.get("goal_id")
        await task_svc.settle_task(ctx, tid, to_status="completed",
                                   result="built it", completed_by="subagent:e4b70123")
        await task_svc.deliver_settlement(ctx, tid)
    assert wake.await_args.kwargs["provenance"] == "task_relay"
    assert "POSTED to this chat" in wake.await_args.args[2]


async def test_conversation_settled_promise_stays_a_quiet_fold(ctx):
    from server.services import tasks as task_svc
    with patch("server.services.wake_service.wake_conversation",
               new=AsyncMock()) as wake:
        t = await task_svc.register_task(ctx, waiter_session=ORIGIN, title="ask Sean")
        tid = t.get("task_id") or t.get("id") or t.get("goal_id")
        await task_svc.settle_task(ctx, tid, to_status="completed", result="Thursday",
                                   completed_by="conversation:agent:main:whatsapp:dm:61480346372")
        await task_svc.deliver_settlement(ctx, tid)
    assert wake.await_args.kwargs["provenance"] == "wake_nudge"
