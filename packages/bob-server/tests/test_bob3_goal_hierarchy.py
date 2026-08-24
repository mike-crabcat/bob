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

from bob_server.repositories.goals import GoalRepository
from bob_server.repositories.wakeups import WakeupRepository
from bob_server.services import goal_service


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
    monkeypatch.setattr("bob_server.services.wake_service.wake_conversation", wake)
    return wake


@pytest.fixture
def reviser(monkeypatch):
    """Mock the reviser LLM call; tests set `.response` (a JSON string)."""
    from bob_server.services.llm_dispatch import LLMDispatchService

    mock = AsyncMock()
    mock.response = _reviser_json({"plan": "updated", "known": ["alice confirmed"]})

    async def _chat(self, messages, **kwargs):
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


async def test_child_settle_rolls_up_without_origin_wake(ctx, db, mock_wake, reviser):
    root, child = await _make_tree(ctx)
    reviser.response = _reviser_json(
        {"plan": "negotiate time", "known": ["alice confirmed 3pm"],
         "open_questions": [], "next_actions": [],
         "refs": {"entities": ["event-team-lunch"], "claims": []}})

    await goal_service.complete_goal(ctx, child["id"], result="alice confirmed 3pm")

    # Child settle never wakes the origin directly.
    mock_wake.assert_not_awaited()

    # The result folded into the parent's state.
    parent = await GoalRepository(db).get(root["id"])
    assert "alice confirmed 3pm" in parent["strategy_json"]

    # The revision ran as a durable, delivered effect.
    eff = await db.fetch_one("SELECT * FROM effects WHERE kind = 'goal_revise_state'")
    assert eff is not None and eff["status"] == "delivered"


async def test_reviser_wakes_parent_working_conversation_not_origin(
        ctx, db, mock_wake, reviser):
    root, child = await _make_tree(ctx)
    reviser.response = _reviser_json(
        {"plan": "x"}, wake_needed=True, summary="alice confirmed — quorum reached")

    await goal_service.complete_goal(ctx, child["id"], result="alice confirmed 3pm")

    mock_wake.assert_awaited_once()
    args = mock_wake.await_args
    assert args.args[1] == "work", "wake lands on the parent's working conversation"
    assert args.kwargs.get("call_category") == "goal_progress"


async def test_root_settle_wakes_origin_once(ctx, db, mock_wake, reviser):
    root, child = await _make_tree(ctx)
    await goal_service.complete_goal(ctx, child["id"], result="done")
    await goal_service.complete_goal(ctx, root["id"], result="lunch booked")

    # Only the root's settle wakes the origin — exactly once.
    mock_wake.assert_awaited_once()
    assert mock_wake.await_args.args[1] == "asker"


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

async def test_reviser_malformed_output_degrades_to_wake(ctx, db, mock_wake, reviser):
    root, _ = await _make_tree(ctx)
    before = (await GoalRepository(db).get(root["id"]))["strategy_json"]
    reviser.response = "not json at all"

    from bob_server.services.goal_state_service import revise_goal_state
    outcome = await revise_goal_state(
        ctx, root["id"], " Stimulus: bob says Tuesday. ",
        stimulus_id="test:malformed")

    assert outcome["outcome"] == "error" and outcome["wake"] == "wake"
    mock_wake.assert_awaited_once()
    assert mock_wake.await_args.args[1] == "work"
    assert "Tuesday" in mock_wake.await_args.args[2], "raw stimulus relayed"
    assert (await GoalRepository(db).get(root["id"]))["strategy_json"] == before


async def test_reviser_cas_conflict_retries(ctx, db, mock_wake, reviser):
    root, _ = await _make_tree(ctx)
    reviser.response = _reviser_json({"plan": "cas-survivor"})

    real_revise = GoalRepository.revise
    calls = {"n": 0}

    async def _flaky_revise(self, goal_id, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulate a concurrent writer bumping the version first.
            await real_revise(self, goal_id, expected_version=kwargs["expected_version"],
                              progress="concurrent touch")
            return False
        return await real_revise(self, goal_id, **kwargs)

    from bob_server.services.goal_state_service import revise_goal_state
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(GoalRepository, "revise", _flaky_revise)
        outcome = await revise_goal_state(ctx, root["id"], "stimulus",
                                          stimulus_id="test:cas")

    assert outcome["outcome"] == "revised"
    row = await GoalRepository(db).get(root["id"])
    assert "cas-survivor" in row["strategy_json"]


async def test_shadow_mode_records_but_suppresses_wake(
        ctx, db, mock_wake, reviser, monkeypatch):
    root, _ = await _make_tree(ctx)
    reviser.response = _reviser_json({"plan": "x"}, wake_needed=True, summary="changed")
    monkeypatch.setenv("BOB_GOAL_STATE_SHADOW", "1")

    from bob_server.services.goal_state_service import revise_goal_state
    outcome = await revise_goal_state(ctx, root["id"], "stimulus",
                                      stimulus_id="test:shadow")
    assert outcome["wake"] == "shadow_wake"
    mock_wake.assert_not_awaited()
    assert "x" in (await GoalRepository(db).get(root["id"]))["strategy_json"]


async def test_legacy_outreach_strategy_reaches_reviser_prompt(
        ctx, db, mock_wake, reviser):
    goal = await goal_service.create_goal(
        ctx, conversation_id="target-dm", objective="ask about the BBQ",
        kind="outreach",
        strategy={"requestor": "Mike", "message": "hey, BBQ sat?"})

    seen: dict = {}

    from bob_server.services.llm_dispatch import LLMDispatchService

    async def _chat(self, messages, **kwargs):
        seen["user"] = messages[-1]["content"]
        return _reviser_json({"plan": "waiting on reply"})

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(LLMDispatchService, "chat", _chat)
        from bob_server.services.goal_state_service import revise_goal_state
        await revise_goal_state(ctx, goal["id"], "stimulus",
                                stimulus_id="test:legacy")

    assert "Mike" in seen["user"] and "BBQ sat?" in seen["user"], \
        "legacy outreach state is wrapped and shown to the reviser"


# ---------------------------------------------------------------------------
# Prompt injection budget (§1.4)
# ---------------------------------------------------------------------------

async def test_goals_block_caps_at_five_goals_and_truncates(ctx, db):
    from bob_server.services.context_assembler import ContextAssembler

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
    from bob_server.services.context_assembler import ContextAssembler
    assert await ContextAssembler(ctx).goals_block("nobody") == ""


# ---------------------------------------------------------------------------
# Tool surface (§1.5)
# ---------------------------------------------------------------------------

async def test_create_goal_tool_with_parent_and_strategy(ctx, db, mock_wake):
    from bob_server.services.goal_tools import make_goal_tools

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
    from bob_server.services.goal_tools import make_goal_tools

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
    from bob_server.services.goal_tools import make_goal_tools

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
    from bob_server.services import phone_call_result_service as prs

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
        from bob_server.repositories.phone_calls import PhoneCallRepository
        mp.setattr(PhoneCallRepository, "get", _fake_get)
        # The child goal is parented: no direct origin wake; roll-up instead.
        woke = await prs._settle_call_goal(ctx, "call-1", "completed",
                                           "## Call Result\nbooked for 7pm")
    assert woke is True
    mock_wake.assert_not_awaited(), "child settle rolls up rather than waking"
    assert (await GoalRepository(db).get(call_goal["id"]))["status"] == "completed"
