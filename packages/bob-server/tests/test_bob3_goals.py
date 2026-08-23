"""Goal + wakeup tests (Bob3 Phase V).

Covers the plan's race fixture requirements: simultaneous completion,
cancel-vs-complete, stale revision — plus deadline wakeups and the
completion→origin-wake path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from bob_server.repositories.goals import GoalRepository
from bob_server.repositories.wakeups import WakeupRepository
from bob_server.services import goal_service


def _past() -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()


def _future() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


async def test_create_and_get(ctx, db):
    goal = await goal_service.create_goal(
        ctx, conversation_id="conv-a", objective="find out the time",
        origin_conversation_id="conv-origin", kind="outreach")
    assert goal["status"] == "active" and goal["version"] == 1
    ev = await db.fetch_one(
        "SELECT * FROM event_log WHERE event_type = 'goal.created'")
    assert ev is not None


async def test_stale_revision_rejected(ctx, db):
    repo = GoalRepository(db)
    goal = await repo.create(conversation_id="c", objective="obj")
    assert await repo.revise(goal["id"], expected_version=1, progress="step 1")
    # Same expected_version again → stale, rejected.
    assert not await repo.revise(goal["id"], expected_version=1, progress="stale")
    row = await repo.get(goal["id"])
    assert row["progress"] == "step 1" and row["version"] == 2


async def test_simultaneous_completion_single_winner(ctx, db, monkeypatch):
    wake = AsyncMock()
    monkeypatch.setattr("bob_server.services.wake_service.wake_conversation", wake)
    goal = await goal_service.create_goal(
        ctx, conversation_id="c", objective="obj", origin_conversation_id="o")

    first = await goal_service.complete_goal(ctx, goal["id"], result="done A")
    second = await goal_service.complete_goal(ctx, goal["id"], result="done B")
    assert first and not second, "exactly one completion wins the CAS"

    row = await GoalRepository(db).get(goal["id"])
    assert row["status"] == "completed" and row["result"] == "done A"
    wake.assert_awaited_once()
    transitions = await GoalRepository(db).transitions(goal["id"])
    assert len(transitions) == 1


async def test_cancel_vs_complete_race(ctx, db, monkeypatch):
    monkeypatch.setattr("bob_server.services.wake_service.wake_conversation", AsyncMock())
    goal = await goal_service.create_goal(ctx, conversation_id="c", objective="obj")

    assert await GoalRepository(db).cancel(goal["id"])
    assert not await goal_service.complete_goal(ctx, goal["id"], result="too late")
    row = await GoalRepository(db).get(goal["id"])
    assert row["status"] == "cancelled"


async def test_completion_wakes_origin_with_result(ctx, db, monkeypatch):
    wake = AsyncMock()
    monkeypatch.setattr("bob_server.services.wake_service.wake_conversation", wake)
    goal = await goal_service.create_goal(
        ctx, conversation_id="conv-work", objective="ask John about Thursday",
        origin_conversation_id="agent:main:whatsapp:group:123")

    await goal_service.complete_goal(ctx, goal["id"], result="John says yes, 3pm")

    args = wake.await_args
    assert args.args[1] == "agent:main:whatsapp:group:123"
    assert "John says yes, 3pm" in args.args[2]
    ev = await db.fetch_one(
        "SELECT * FROM event_log WHERE event_type = 'goal.completed'")
    assert ev is not None


async def test_no_wake_when_origin_is_same_conversation(ctx, db, monkeypatch):
    wake = AsyncMock()
    monkeypatch.setattr("bob_server.services.wake_service.wake_conversation", wake)
    goal = await goal_service.create_goal(
        ctx, conversation_id="c", objective="obj", origin_conversation_id="c")
    await goal_service.complete_goal(ctx, goal["id"], result="done")
    wake.assert_not_awaited()


async def test_deadline_schedules_wakeup_and_settle_cancels_it(ctx, db, monkeypatch):
    monkeypatch.setattr("bob_server.services.wake_service.wake_conversation", AsyncMock())
    goal = await goal_service.create_goal(
        ctx, conversation_id="c", objective="obj",
        origin_conversation_id="o", deadline=_future())

    wakeups = await WakeupRepository(db).list_scheduled("o")
    assert len(wakeups) == 1 and wakeups[0]["goal_id"] == goal["id"]

    await goal_service.complete_goal(ctx, goal["id"], result="done")
    assert await WakeupRepository(db).list_scheduled("o") == []


async def test_due_wakeup_fires_with_goal_context(ctx, db, monkeypatch):
    wake = AsyncMock()
    monkeypatch.setattr("bob_server.services.wake_service.wake_conversation", wake)
    goal = await goal_service.create_goal(
        ctx, conversation_id="c", objective="chase the invoice",
        origin_conversation_id="o", deadline=_past())

    fired = await goal_service.pump_due_wakeups(ctx)
    assert fired == 1
    args = wake.await_args
    assert args.args[1] == "o"
    assert "chase the invoice" in args.args[2]

    # Claimed exactly once: a second pump finds nothing.
    assert await goal_service.pump_due_wakeups(ctx) == 0
    assert goal["id"]  # goal untouched by deadline fire
    row = await GoalRepository(db).get(goal["id"])
    assert row["status"] == "active"


async def test_wakeup_for_settled_goal_is_moot(ctx, db, monkeypatch):
    wake = AsyncMock()
    monkeypatch.setattr("bob_server.services.wake_service.wake_conversation", wake)
    goal = await GoalRepository(db).create(conversation_id="c", objective="obj")
    await WakeupRepository(db).schedule(
        conversation_id="c", not_before=_past(), goal_id=goal["id"])
    await GoalRepository(db).cancel(goal["id"])

    fired = await goal_service.pump_due_wakeups(ctx)
    assert fired == 1  # claimed, but delivery skipped
    wake.assert_not_awaited()


async def test_recurring_wakeup_reschedules(ctx, db, monkeypatch):
    monkeypatch.setattr("bob_server.services.wake_service.wake_conversation", AsyncMock())
    await WakeupRepository(db).schedule(
        conversation_id="c", not_before=_past(), recurrence="+30m")

    await goal_service.pump_due_wakeups(ctx)
    scheduled = await WakeupRepository(db).list_scheduled("c")
    assert len(scheduled) == 1 and scheduled[0]["recurrence"] == "+30m"


async def test_goal_tools_create_and_complete_via_effects(ctx, db, monkeypatch):
    import json as _json

    wake = AsyncMock()
    monkeypatch.setattr("bob_server.services.wake_service.wake_conversation", wake)
    from bob_server.services.goal_tools import make_goal_tools

    tools = {t.name: t for t in make_goal_tools(ctx, "agent:main:whatsapp:dm:1")}
    out = _json.loads(await tools["create_goal"].handler(objective="test obj"))
    assert out["ok"]
    goal_id = out["goal_id"]

    listed = _json.loads(await tools["list_goals"].handler())
    assert [g["goal_id"] for g in listed["goals"]] == [goal_id]

    out = _json.loads(await tools["complete_goal"].handler(
        goal_id=goal_id, result="all done"))
    assert out["ok"]
    row = await GoalRepository(db).get(goal_id)
    assert row["status"] == "completed"
    eff = await db.fetch_one(
        "SELECT * FROM effects WHERE kind = 'goal_complete'")
    assert eff["status"] == "delivered"

    # Completing again reports already-settled.
    out = _json.loads(await tools["complete_goal"].handler(
        goal_id=goal_id, result="again"))
    assert out["ok"], "duplicate idempotency key is suppressed as success"


async def test_subagent_result_settles_goal_and_wakes_parent(ctx, db, monkeypatch):
    """First subagent result completes the linked goal → origin wake; a
    follow-up result (goal settled) wakes the parent directly."""
    wake = AsyncMock()
    monkeypatch.setattr("bob_server.services.wake_service.wake_conversation", wake)
    from bob_server.services.subagent_service import SubagentService

    parent = "agent:main:whatsapp:dm:555"
    await db.execute(
        """INSERT INTO subagents (id, parent_session_key, session_key, task,
           status, agent_type, created_at, updated_at)
           VALUES ('sub-1', ?, 'subagent:x:1', 'do the thing', 'running',
                   'claude', '2026-01-01', '2026-01-01')""",
        (parent,),
    )
    await goal_service.create_goal(
        ctx, conversation_id="subagent:x:1", objective="do the thing",
        origin_conversation_id=parent, kind="subagent", external_ref="sub-1")

    svc = SubagentService(ctx)
    await svc._notify_parent("sub-1", "the result")

    goal = await GoalRepository(db).get_by_external_ref("sub-1")
    assert goal["status"] == "completed"
    assert wake.await_count == 1
    assert wake.await_args.args[1] == parent
    assert "the result" in wake.await_args.args[2]

    # Follow-up: goal already settled → direct wake.
    await svc._notify_parent("sub-1", "more results")
    assert wake.await_count == 2
    assert "more results" in wake.await_args.args[2]


async def test_subagent_failure_fails_goal(ctx, db, monkeypatch):
    wake = AsyncMock()
    monkeypatch.setattr("bob_server.services.wake_service.wake_conversation", wake)
    from bob_server.services.subagent_service import SubagentService

    await db.execute(
        """INSERT INTO subagents (id, parent_session_key, session_key, task,
           status, agent_type, created_at, updated_at)
           VALUES ('sub-2', 'agent:main:email:dm:a', 'subagent:x:2', 't',
                   'running', 'claude', '2026-01-01', '2026-01-01')""",
    )
    await goal_service.create_goal(
        ctx, conversation_id="subagent:x:2", objective="t",
        origin_conversation_id="agent:main:email:dm:a", kind="subagent",
        external_ref="sub-2")

    await SubagentService(ctx)._notify_parent("sub-2", "ERROR: boom", failed=True)
    goal = await GoalRepository(db).get_by_external_ref("sub-2")
    assert goal["status"] == "failed"
    wake.assert_awaited_once()
    assert wake.await_args.args[1] == "agent:main:email:dm:a"


async def test_outreach_state_lives_on_goal(ctx, db):
    """Increment 3: the outreach prompt and reply-tool gate read the active
    outreach goal, not route metadata; settling clears them."""
    from bob_server.services.context_assembler import ContextAssembler
    from bob_server.services.goal_service import create_goal, settle_goal

    target = "agent:main:whatsapp:dm:61400000099"
    goal = await create_goal(
        ctx, conversation_id=target, objective="ask about the BBQ",
        origin_conversation_id="agent:main:whatsapp:dm:61400000001",
        kind="outreach",
        strategy={"requestor": "Mike", "message": "hey, BBQ sat?"})

    prompt = await ContextAssembler(ctx).outreach_prompt(target)
    assert "Active Outreach Request" in prompt
    assert "Mike" in prompt and "ask about the BBQ" in prompt and "BBQ sat?" in prompt

    await settle_goal(ctx, goal["id"], status="completed", result="done")
    assert await ContextAssembler(ctx).outreach_prompt(target) == ""
