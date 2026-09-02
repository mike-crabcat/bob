"""Approvals + goal templates (bob-events-plan.md Phase 3).

The vendor integration is deliberately absent: approvals record human
decisions and wake the asker — acting on an approval (placing an order via
a skill) is the agent's job, not the platform's. The template's merch child
points at the workspace skill, with approvals as judgment rather than a
hard gate.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from server.repositories.approvals import ApprovalRepository
from server.repositories.goals import GoalRepository


@pytest.fixture
def mock_wake(monkeypatch):
    wake = AsyncMock()
    monkeypatch.setattr("server.services.wake_service.wake_conversation", wake)
    return wake


async def _purchase_approval(ctx, *, goal_id=None) -> str:
    row = await ApprovalRepository(ctx.db).create(
        approval_type="purchase", entity_id=goal_id or "goal-x",
        title="Team lunch merch order",
        description="8 × team tee",
        proposal={"goal_id": goal_id, "summary": "8 × team tee, ~$160",
                  "order": {"recipient": {"name": "Mike"},
                            "items": [{"variant_id": 42, "quantity": 8,
                                       "name": "team tee",
                                       "retail_price": 20.0}]}})
    return row["id"]


# ---------------------------------------------------------------------------
# Approvals repository + migration 460
# ---------------------------------------------------------------------------

async def test_purchase_approval_type_and_cas_respond(ctx, db):
    repo = ApprovalRepository(db)
    row = await repo.create(approval_type="purchase", entity_id="g1",
                            title="cart", requested_by="bob")
    assert row["status"] == "pending" and row["approval_type"] == "purchase"

    approved = await repo.respond(row["id"], "approved", reviewed_by="mike")
    assert approved["status"] == "approved"
    assert approved["reviewed_by"] == "mike"

    # CAS: a second respond on the settled row is rejected.
    assert await repo.respond(row["id"], "rejected") is None


async def test_pending_view_and_list(ctx, db):
    repo = ApprovalRepository(db)
    await repo.create(approval_type="purchase", entity_id="g1", title="a")
    assert len(await repo.pending()) == 1
    row = await db.fetch_one("SELECT COUNT(*) AS n FROM pending_approvals")
    assert row["n"] == 1


# ---------------------------------------------------------------------------
# Approval tools — record decisions, wake the asker, chain nothing
# ---------------------------------------------------------------------------

async def test_approval_request_wakes_origin_with_summary(ctx, db, mock_wake):
    from server.services.approval_tools import make_approval_tools

    tools = {t.name: t for t in make_approval_tools(ctx, "conv-origin")}
    out = json.loads(await tools["request_approval"].handler(
        title="Team lunch merch order", description="8 × team tee",
        approval_json=json.dumps({
            "approval_type": "purchase", "entity_id": "goal-9",
            "origin_conversation_id": "conv-origin",
            "proposal": {"summary": "4 × tee",
                         "order": {"items": [{"variant_id": 1, "quantity": 4,
                                              "name": "tee",
                                              "retail_price": 20.0}]}}})))
    assert out["ok"], out
    mock_wake.assert_awaited_once()
    args = mock_wake.await_args
    assert args.args[1] == "conv-origin"
    assert "4 × tee" in args.args[2] and "$80.00" in args.args[2]


async def test_approve_records_decision_and_chains_no_effects(ctx, db, mock_wake):
    """Approving settles the approval row only — placing any order is the
    agent's job (via its skills) on the wake that follows. No vendor effect
    may exist in the platform."""
    approval_id = await _purchase_approval(ctx)
    from server.services.approval_tools import make_approval_tools
    tools = {t.name: t for t in make_approval_tools(ctx, "conv")}

    out = json.loads(await tools["respond_approval"].handler(
        approval_id=approval_id, decision="approve"))
    assert out["ok"] and out["status"] == "approved"
    rows = await db.fetch_all(
        "SELECT DISTINCT kind FROM effects WHERE kind NOT LIKE 'goal%' "
        "AND kind NOT IN ('approval_request', 'approval_respond')")
    assert rows == [], f"no vendor/order effects expected, got {rows}"


async def test_reject_records_without_side_effects(ctx, db, mock_wake):
    approval_id = await _purchase_approval(ctx)
    from server.services.approval_tools import make_approval_tools
    tools = {t.name: t for t in make_approval_tools(ctx, "conv")}

    out = json.loads(await tools["respond_approval"].handler(
        approval_id=approval_id, decision="reject"))
    assert out["ok"] and out["status"] == "rejected"
    row = await ApprovalRepository(db).get(approval_id)
    assert row["reviewed_by"] == "conv"


# ---------------------------------------------------------------------------
# Goal templates (§3.5)
# ---------------------------------------------------------------------------

async def test_instantiate_team_event_template(ctx, db, mock_wake):
    from server.services.goal_templates import (
        instantiate_template, load_templates,
    )

    assert "team-event" in load_templates(ctx.settings.config_dir)

    result = await instantiate_template(
        ctx, template_name="team-event",
        session_key="agent:main:whatsapp:dm:61400000001",
        params={"event_name": "Team Lunch", "group_name": "AI Doom",
                "group_session_key": "agent:main:whatsapp:group:doom",
                "decide_by": "2026-09-05T17:00:00+00:00"})

    repo = GoalRepository(db)
    root = await repo.get(result["root_goal_id"])
    assert root["kind"] == "event_plan"
    assert "Team Lunch" in root["objective"] and "AI Doom" in root["objective"]
    strategy = json.loads(root["strategy_json"])
    assert "group-ai-doom" in strategy["refs"]["entities"], "derived slug"
    assert "event-team-lunch" in strategy["refs"]["entities"]

    kids = {g["id"]: g for g in await repo.children_of(root["id"])}
    assert set(result["children"].values()) == set(kids)
    negotiate = await repo.get(result["children"]["negotiate"])
    assert negotiate["kind"] == "negotiate"
    neg_strategy = json.loads(negotiate["strategy_json"])
    assert neg_strategy["decision"]["quorum"] == 0.75
    assert neg_strategy["decision"]["of"] == "invitees"
    # Children inherit the root's refs so router ref-matches reach the child
    # accumulating the information (attendance must not fragment).
    assert "event-team-lunch" in neg_strategy["refs"]["entities"]

    # Group chat registered as holder → routing + seeding cover group replies.
    holders = {h["conversation_id"]: h["role"]
               for h in await repo.holders_of(root["id"])}
    assert holders.get("agent:main:whatsapp:group:doom") == "holder"

    held = await repo.goals_held_by("agent:main:whatsapp:group:doom")
    assert any(g["id"] == root["id"] for g in held)

    # The merch child points at the skill, not a platform vendor.
    merch = await repo.get(result["children"]["merch"])
    merch_strategy = json.loads(merch["strategy_json"])
    assert "skills/printful" in merch_strategy["how"]


async def test_instantiate_missing_params_lists_them(ctx, db, mock_wake):
    from server.services.goal_templates import instantiate_template
    with pytest.raises(ValueError, match="group_session_key"):
        await instantiate_template(
            ctx, template_name="team-event", session_key="work",
            params={"event_name": "x", "group_name": "y"})


async def test_template_tools_roundtrip(ctx, db, mock_wake):
    from server.services.goal_tools import make_goal_tools

    tools = {t.name: t for t in make_goal_tools(ctx, "work")}
    listed = json.loads(await tools["list_goal_templates"].handler())
    assert any(t["name"] == "team-event" for t in listed["templates"])

    out = json.loads(await tools["instantiate_goal_template"].handler(
        template="team-event",
        params_json=json.dumps({
            "event_name": "Doom Lunch", "group_name": "AI Doom",
            "group_session_key": "agent:main:whatsapp:group:g1",
            "decide_by": "2026-09-05T17:00:00+00:00"})))
    assert out["ok"] and out["children"]["book"]

    bad = json.loads(await tools["instantiate_goal_template"].handler(
        template="nope", params_json="{}"))
    assert not bad["ok"]


async def test_outreach_tool_parents_under_goal(ctx, db, mock_wake):
    """The negotiation fan-out's outreach goals roll up into the plan."""
    from server.services import goal_service
    root = await goal_service.create_goal(
        ctx, conversation_id="work", objective="plan lunch",
        strategy={"v": 2, "refs": {"entities": ["group-ai-doom"]}})

    from server.services.whatsapp_outreach_tools import (
        make_whatsapp_outreach_tools,
    )

    class _FakeBridge:
        connected = True

        async def send_message(self, jid, message):
            return "req-1"

    tools = {t.name: t for t in make_whatsapp_outreach_tools(
        ctx, _FakeBridge(), "work")}

    await db.execute(
        "INSERT INTO contacts (id, name, phone_number, created_at, updated_at) "
        "VALUES ('c-alice', 'Alice', '+61400000002', datetime('now'), datetime('now'))")
    out = json.loads(await tools["send_whatsapp_to_contact"].handler(
        contact_id="c-alice", message="hey, lunch Thursday?",
        objective="Get Alice's availability and t-shirt size",
        parent_goal_id=root["id"]))
    assert out["ok"]

    goals = await GoalRepository(db).children_of(root["id"])
    outreach = [g for g in goals if g["kind"] == "outreach"]
    assert outreach and outreach[0]["parent_goal_id"] == root["id"]
