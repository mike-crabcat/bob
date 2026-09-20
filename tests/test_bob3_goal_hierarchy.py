"""Goal hierarchy + state reviser tests (bob-events-plan.md Phase 1).

Covers the plan's test requirements: repo CRUD for hierarchy/holders, the
settle roll-up (child → parent reviser, never a direct origin wake), the
wake matrix (deadline retargeting to the root's working conversation), the
revise_goal_state contract (validation retry, CAS retry, degrade-to-wake,
shadow mode, legacy strategy wrap), prompt-injection budget, and the
extended tool surface.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from server.repositories.goals import GoalRepository
from server.repositories.wakeups import WakeupRepository
from server.services import goal_service


@pytest.fixture(autouse=True)
def _legacy_goal_path(ctx):
    """These tests pin the LEGACY goal machinery (reviser, wake matrix,
    claim-router delivery) — the fallback path under goal rooms. Rooms have
    their own suite: tests/services/test_goal_rooms.py."""
    ctx.settings.goal_rooms.enabled = False
    yield


def _past() -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()


def _future() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


def _reviser_json(state: dict, *, wake_needed: bool = False,
                  summary: str = "") -> str:
    return json.dumps({
        "state": {"v": 2, **state},
        "wake_needed": wake_needed,
        "wake_summary": summary,
    })


@pytest.fixture
def mock_wake(monkeypatch):
    wake = AsyncMock()
    monkeypatch.setattr("server.services.wake_service.wake_conversation", wake)
    return wake


@pytest.fixture
def reviser(monkeypatch):
    """Mock the reviser LLM call; tests set `.response` (a JSON string).
    `.calls` records each call's kwargs (max_tokens, reasoning_effort…)."""
    from server.services.llm_dispatch import LLMDispatchService

    mock = AsyncMock()
    mock.response = _reviser_json({"plan": "updated", "known": ["alice confirmed"]})
    mock.calls = []

    async def _chat(self, messages, **kwargs):
        mock.calls.append(kwargs)
        return mock.response

    monkeypatch.setattr(LLMDispatchService, "chat", _chat)
    return mock


# ---------------------------------------------------------------------------
# Repo: hierarchy + holders
# ---------------------------------------------------------------------------

async def test_hierarchy_repo_operations(ctx, db):
    repo = GoalRepository(db)
    parent = await repo.create(conversation_id="work", objective="plan lunch")
    child_a = await repo.create(conversation_id="dm-a", objective="ask alice",
                                parent_goal_id=parent["id"])
    child_b = await repo.create(conversation_id="dm-b", objective="ask bob",
                                parent_goal_id=parent["id"])

    kids = await repo.children_of(parent["id"])
    assert {k["id"] for k in kids} == {child_a["id"], child_b["id"]}
    assert await repo.children_of(parent["id"], status="completed") == []

    assert (await repo.root_of(child_a["id"]))["id"] == parent["id"]
    assert (await repo.root_of(parent["id"]))["id"] == parent["id"]

    await repo.add_holder(parent["id"], "group-ai-doom", role="holder")
    holders = {(h["conversation_id"], h["role"]) for h in await repo.holders_of(parent["id"])}
    assert ("group-ai-doom", "holder") in holders

    held = await repo.goals_held_by("group-ai-doom")
    assert [g["id"] for g in held] == [parent["id"]]


async def test_create_goal_registers_holders_with_cids(ctx, db):
    goal = await goal_service.create_goal(
        ctx, conversation_id="agent:main:whatsapp:dm:111",
        objective="obj", origin_conversation_id="agent:main:whatsapp:dm:222")
    roles = {h["conversation_id"]: h["role"] for h in await GoalRepository(db).holders_of(goal["id"])}
    assert roles == {"agent:main:whatsapp:dm:111": "worker",
                     "agent:main:whatsapp:dm:222": "origin"}


# ---------------------------------------------------------------------------
# Settle roll-up (wake matrix)
# ---------------------------------------------------------------------------

async def _make_tree(ctx):
    root = await goal_service.create_goal(
        ctx, conversation_id="work", objective="plan the lunch",
        origin_conversation_id="asker",
        strategy={"v": 2, "plan": "negotiate time", "known": [],
                  "open_questions": [], "next_actions": [],
                  "refs": {"entities": ["event-team-lunch"], "claims": []}})
    child = await goal_service.create_goal(
        ctx, conversation_id="agent:main:whatsapp:dm:333",
        objective="ask alice", origin_conversation_id="asker",
        parent_goal_id=root["id"])
    return root, child


async def test_child_settle_rolls_up_to_parent_working_convo(ctx, db, mock_wake):
    """Phase 4 (reviser retired): a child settle wakes the PARENT's working
    conversation with the outcome — direct, no reviser fold — and never the
    origin (the origin hears only from the root's own settle)."""
    root, child = await _make_tree(ctx)
    await goal_service.complete_goal(ctx, child["id"], result="alice confirmed 3pm")

    targets = [c.args[1] for c in mock_wake.await_args_list]
    assert "asker" not in targets            # origin untouched by child settle
    assert "work" in targets                 # parent working convo informed
    progress = [c for c in mock_wake.await_args_list if c.args[1] == "work"]
    assert "alice confirmed 3pm" in progress[0].args[2]


async def test_child_deadline_wakes_root_working_conversation(ctx, db, mock_wake):
    root = await goal_service.create_goal(
        ctx, conversation_id="work", objective="plan", origin_conversation_id="asker")
    child = await goal_service.create_goal(
        ctx, conversation_id="agent:main:whatsapp:dm:333", objective="ask alice",
        origin_conversation_id="asker", parent_goal_id=root["id"], deadline=_past())

    # Scheduled on the root's working conversation, not the child's DM.
    assert len(await WakeupRepository(db).list_scheduled("work")) == 1
    assert await WakeupRepository(db).list_scheduled("agent:main:whatsapp:dm:333") == []

    fired = await goal_service.pump_due_wakeups(ctx)
    assert fired == 1
    assert mock_wake.await_args.args[1] == "work"


async def test_settled_child_wakeup_cancelled(ctx, db, mock_wake, reviser):
    root = await goal_service.create_goal(
        ctx, conversation_id="work", objective="plan", origin_conversation_id="asker")
    child = await goal_service.create_goal(
        ctx, conversation_id="dm-a", objective="ask", parent_goal_id=root["id"],
        deadline=_future())
    await goal_service.complete_goal(ctx, child["id"], result="done")
    assert await WakeupRepository(db).list_scheduled("work") == []


# ---------------------------------------------------------------------------
# revise_goal_state contract
# ---------------------------------------------------------------------------

async def test_goals_block_caps_at_five_goals_and_truncates(ctx, db):
    from server.services.context_assembler import ContextAssembler

    long_plan = "word " * 400
    for i in range(7):
        await goal_service.create_goal(
            ctx, conversation_id="work", objective=f"goal {i}",
            strategy={"v": 2, "plan": long_plan if i == 6 else f"plan {i}"})

    block = await ContextAssembler(ctx).goals_block("work")
    assert block.count("###") == 5, "top-5 by recency only"
    assert "goal 6" in block and "goal 0" not in block
    rendered_plan = [ln for ln in block.splitlines() if ln.startswith("Plan:")]
    assert rendered_plan and len(rendered_plan[0]) <= 250, "plan truncated to budget"


async def test_goals_block_empty_without_goals(ctx, db):
    from server.services.context_assembler import ContextAssembler
    assert await ContextAssembler(ctx).goals_block("nobody") == ""


# ---------------------------------------------------------------------------
# Tool surface (§1.5)
# ---------------------------------------------------------------------------

async def test_create_goal_tool_with_parent_and_strategy(ctx, db, mock_wake):
    from server.services.goal_tools import make_goal_tools

    tools = {t.name: t for t in make_goal_tools(ctx, "work")}
    root_out = json.loads(await tools["create_goal"].handler(
        objective="plan the lunch", kind="event_plan",
        strategy=json.dumps({"plan": "ask everyone",
                             "refs": {"entities": ["event-team-lunch"]}})))
    assert root_out["ok"]
    root_id = root_out["goal_id"]

    child_out = json.loads(await tools["create_goal"].handler(
        objective="negotiate the time", kind="negotiate",
        parent_goal_id=root_id,
        strategy=json.dumps({"known": ["8 invitees"]})))
    assert child_out["ok"]

    repo = GoalRepository(db)
    child = await repo.get(child_out["goal_id"])
    assert child["parent_goal_id"] == root_id
    state = json.loads(child["strategy_json"])
    assert state["v"] == 2 and state["known"] == ["8 invitees"]

    # Invalid parent rejected.
    bad = json.loads(await tools["create_goal"].handler(
        objective="x", parent_goal_id="nonexistent"))
    assert not bad["ok"]


async def test_update_goal_state_tool_cas_write(ctx, db, mock_wake):
    from server.services.goal_tools import make_goal_tools

    tools = {t.name: t for t in make_goal_tools(ctx, "work")}
    goal_id = json.loads(await tools["create_goal"].handler(objective="obj"))["goal_id"]

    out = json.loads(await tools["update_goal_state"].handler(
        goal_id=goal_id, expected_version=1,
        state=json.dumps({"plan": "v2 plan", "known": ["a", "b"],
                          "next_actions": [{"action": "chase carol",
                                            "due": "2026-08-26T10:00:00+00:00"}]})))
    assert out["ok"]
    row = await GoalRepository(db).get(goal_id)
    state = json.loads(row["strategy_json"])
    assert state["plan"] == "v2 plan"
    assert state["next_actions"][0]["action"] == "chase carol"

    # Stale version rejected.
    stale = json.loads(await tools["update_goal_state"].handler(
        goal_id=goal_id, expected_version=1, state=json.dumps({"plan": "stale"})))
    assert not stale["ok"]

    # Schema violations rejected before any write.
    bad = json.loads(await tools["update_goal_state"].handler(
        goal_id=goal_id, expected_version=2,
        state=json.dumps({"next_actions": [{"due": "no action key"}]})))
    assert not bad["ok"] and "schema" in bad["error"]


async def test_schedule_goal_wakeup_tool_targets_root(ctx, db, mock_wake):
    from server.services.goal_tools import make_goal_tools

    tools = {t.name: t for t in make_goal_tools(ctx, "work")}
    root_id = json.loads(await tools["create_goal"].handler(objective="root"))["goal_id"]
    child_out = json.loads(await tools["create_goal"].handler(
        objective="child", parent_goal_id=root_id))
    child_id = child_out["goal_id"]

    when = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    out = json.loads(await tools["schedule_goal_wakeup"].handler(
        goal_id=child_id, not_before=when, note="T-24h reminder"))
    assert out["ok"]

    scheduled = await WakeupRepository(db).list_scheduled("work")
    assert len(scheduled) == 1 and scheduled[0]["goal_id"] == child_id


# ---------------------------------------------------------------------------
# Phone-result fold (§1.2 chokepoint)
# ---------------------------------------------------------------------------

async def test_call_result_wake_rides_settle_chokepoint(ctx, db, mock_wake, reviser):
    from server.services import phone_call_result_service as prs

    root = await goal_service.create_goal(
        ctx, conversation_id="work", objective="book venue",
        origin_conversation_id="asker")
    call_goal = await goal_service.create_goal(
        ctx, conversation_id="subagent:work:1", objective="call the restaurant",
        origin_conversation_id="work", kind="call", external_ref="sub-9",
        parent_goal_id=root["id"])

    async def _fake_get(self, call_id):
        return {"subagent_id": "sub-9"}

    with pytest.MonkeyPatch.context() as mp:
        from server.repositories.phone_calls import PhoneCallRepository
        mp.setattr(PhoneCallRepository, "get", _fake_get)
        # The child goal is parented: no direct origin wake; roll-up instead.
        woke = await prs._settle_call_goal(ctx, "call-1", "completed",
                                           "## Call Result\nbooked for 7pm")
    assert woke is True
    # Phase 4: the rollup is a direct wake of the PARENT's working
    # conversation — the origin is never touched by a child settle.
    targets = [c.args[1] for c in mock_wake.await_args_list]
    assert "asker" not in targets
    assert "work" in targets
    assert (await GoalRepository(db).get(call_goal["id"]))["status"] == "completed"


# ---------------------------------------------------------------------------
# Progress-review loop (§4.1)
# ---------------------------------------------------------------------------

@pytest.fixture
def review_task(monkeypatch):
    from server import heartbeat
    monkeypatch.setattr(heartbeat, "_last_goal_review", None)
    monkeypatch.delenv("BOB_GOAL_REVIEW_DISABLED", raising=False)
    return heartbeat.GoalReviewTask()


async def _age_goal(db, goal_id: str, hours: float = 48) -> None:
    from datetime import datetime, timezone
    old = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    await db.execute("UPDATE goals SET updated_at = ? WHERE id = ?",
                     (old, goal_id))

