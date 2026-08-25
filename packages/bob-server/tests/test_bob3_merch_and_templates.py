"""Payment gate + merch ordering + goal templates (bob-events-plan.md Phase 3).

The gate's hard property: nothing spends without a recorded human approval,
and the order executor independently re-checks it.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from bob_server.repositories.approvals import ApprovalRepository
from bob_server.repositories.goals import GoalRepository


@pytest.fixture
def mock_wake(monkeypatch):
    wake = AsyncMock()
    monkeypatch.setattr("bob_server.services.wake_service.wake_conversation", wake)
    return wake


async def _purchase_approval(ctx, *, cart=None, goal_id=None) -> str:
    row = await ApprovalRepository(ctx.db).create(
        approval_type="purchase", entity_id=goal_id or "goal-x",
        title="Team lunch merch order",
        description="8 × team tee",
        proposal={"goal_id": goal_id, "summary": "8 × team tee, ~$160",
                  "order": cart or {
                      "recipient": {"name": "Mike", "address1": "1 Main St"},
                      "items": [{"variant_id": 42, "quantity": 8,
                                 "name": "team tee", "retail_price": 20.0}]}})
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
# respond_approval tool + order chaining
# ---------------------------------------------------------------------------

async def test_approve_chains_merch_order_effect(ctx, db, mock_wake, monkeypatch):
    ctx.settings.merch.enabled = True
    approval_id = await _purchase_approval(ctx)

    placed: dict = {}

    async def _fake_place(ctx_, *, approval_id, order):
        placed["approval_id"] = approval_id
        placed["order"] = order
        return "pod-123"

    from bob_server.services import merch_service
    monkeypatch.setattr(merch_service, "place_order", _fake_place)

    from bob_server.services.approval_tools import make_approval_tools
    tools = {t.name: t for t in make_approval_tools(ctx, "agent:main:whatsapp:dm:1")}

    listed = json.loads(await tools["list_pending_approvals"].handler())
    assert listed["pending"][0]["summary"] == "8 × team tee, ~$160"

    out = json.loads(await tools["respond_approval"].handler(
        approval_id=approval_id, decision="approve"))
    assert out["ok"] and out["status"] == "approved"
    assert placed["approval_id"] == approval_id
    assert placed["order"]["items"][0]["variant_id"] == 42

    eff = await db.fetch_one("SELECT * FROM effects WHERE kind = 'merch_order'")
    assert eff is not None and eff["status"] == "delivered"


async def test_reject_does_not_order(ctx, db, mock_wake):
    approval_id = await _purchase_approval(ctx)
    from bob_server.services.approval_tools import make_approval_tools
    tools = {t.name: t for t in make_approval_tools(ctx, "conv")}

    out = json.loads(await tools["respond_approval"].handler(
        approval_id=approval_id, decision="reject"))
    assert out["ok"] and out["status"] == "rejected"
    assert await db.fetch_one(
        "SELECT 1 FROM effects WHERE kind = 'merch_order'") is None


async def test_merch_order_never_spends_without_recorded_approval(
        ctx, db, mock_wake):
    from bob_server.services import merch_service

    approval_id = await _purchase_approval(ctx)  # still pending
    with pytest.raises(RuntimeError, match="not approved"):
        await merch_service.place_order(
            ctx, approval_id=approval_id, order={"items": []})

    # Approved but merch disabled → also refuses (no account configured).
    await ApprovalRepository(db).respond(approval_id, "approved")
    with pytest.raises(RuntimeError, match="disabled"):
        await merch_service.place_order(
            ctx, approval_id=approval_id, order={"items": []})


async def test_merch_order_api_key_from_config_file(ctx, db, mock_wake,
                                                    monkeypatch, tmp_path):
    ctx.settings.merch.enabled = True
    approval_id = await _purchase_approval(ctx)
    await ApprovalRepository(db).respond(approval_id, "approved")

    # Keep the test off the real ~/config — the key file is tmp-scoped and
    # the config_dir override keeps any other config lookup sandboxed too.
    monkeypatch.setattr(ctx.settings, "config_dir", tmp_path)
    key_file = tmp_path / "printful_api_key"
    key_file.write_text("  sekret-key  \n")

    seen: dict = {}

    class _FakeResp:
        status_code = 201
        text = ""
        def json(self):
            return {"result": {"id": 555001}}

    class _FakeClient:
        def __init__(self, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, *, headers=None, json=None):
            seen["url"] = url
            seen["auth"] = headers.get("Authorization")
            seen["json"] = json
            return _FakeResp()

    import bob_server.services.merch_service as ms
    monkeypatch.setattr(ms.httpx, "AsyncClient", _FakeClient, raising=False)

    order_id = await ms.place_order(
        ctx, approval_id=approval_id, order={"items": [{"variant_id": 9}]})
    assert order_id == "555001"
    assert seen["url"].endswith("/v2/orders")
    assert seen["auth"] == "Bearer sekret-key", "key read from file, trimmed"
    assert seen["json"]["external_id"] == f"bob-{approval_id}", \
        "external_id idempotency on the vendor side"

    key_file.unlink()


async def test_merch_order_settles_linked_goal_through_chokepoint(
        ctx, db, mock_wake, monkeypatch):
    from bob_server.services import goal_service, merch_service

    root = await goal_service.create_goal(
        ctx, conversation_id="work", objective="plan", origin_conversation_id="asker")
    merch_child = await goal_service.create_goal(
        ctx, conversation_id="work", objective="produce merch",
        parent_goal_id=root["id"])
    approval_id = await _purchase_approval(ctx, goal_id=merch_child["id"])

    async def _fake_place(ctx_, *, approval_id, order):
        return "pod-9"

    monkeypatch.setattr(merch_service, "place_order", _fake_place)
    from bob_server.services.approval_tools import make_approval_tools
    tools = {t.name: t for t in make_approval_tools(ctx, "asker")}
    out = json.loads(await tools["respond_approval"].handler(
        approval_id=approval_id, decision="approve"))
    assert out["ok"]

    row = await GoalRepository(db).get(merch_child["id"])
    assert row["status"] == "completed"
    assert "pod-9" in row["result"]


# ---------------------------------------------------------------------------
# Goal templates (§3.5)
# ---------------------------------------------------------------------------

async def test_instantiate_team_event_template(ctx, db, mock_wake):
    from bob_server.services.goal_templates import (
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

    from bob_server.repositories.goals import GoalRepository as GR
    held = await GR(db).goals_held_by("agent:main:whatsapp:group:doom")
    assert any(g["id"] == root["id"] for g in held)


async def test_instantiate_missing_params_lists_them(ctx, db, mock_wake):
    from bob_server.services.goal_templates import instantiate_template
    with pytest.raises(ValueError, match="group_session_key"):
        await instantiate_template(
            ctx, template_name="team-event", session_key="work",
            params={"event_name": "x", "group_name": "y"})


async def test_template_tools_roundtrip(ctx, db, mock_wake):
    from bob_server.services.goal_tools import make_goal_tools

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
    from bob_server.services import goal_service
    root = await goal_service.create_goal(
        ctx, conversation_id="work", objective="plan lunch",
        strategy={"v": 2, "refs": {"entities": ["group-ai-doom"]}})

    from bob_server.services.whatsapp_outreach_tools import (
        make_whatsapp_outreach_tools,
    )

    class _FakeBridge:
        connected = True

        async def send_message(self, jid, message):
            return "req-1"

    tools = {t.name: t for t in make_whatsapp_outreach_tools(
        ctx, _FakeBridge(), "work")}

    # Contact for the fan-out target.
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
