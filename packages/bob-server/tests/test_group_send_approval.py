"""Approval-gated cross-group WhatsApp messages.

Every message a trusted contact requests to a different group is approved
individually by the owner (contacts.is_default) in their DM; approval
delivers the stored proposal verbatim via a deterministic, idempotency-keyed
whatsapp_send effect. Approving never flips the target group's
``group_outbound_enabled`` policy — there is no standing consent.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

# Importing approval_tools registers the approval effect executors and (via
# its registration tail) the group_send on-approved hook — in production the
# bridge's tool assembly guarantees this before any request can run.
from bob_server.services import approval_tools  # noqa: F401

from bob_server.repositories.approvals import ApprovalRepository

OWNER_DM = "agent:main:whatsapp:dm:61456224867"
GID = "120363401238199025"
GROUP_KEY = f"agent:main:whatsapp:group:{GID}"
GROUP_NAME = "Leeming Boys"
ORIGIN = "agent:main:whatsapp:group:120363408889690088"  # Pirate Radio (label only)
MESSAGE = "Jam at the shed, Saturday 2pm — bring an amp"


@pytest.fixture(autouse=True)
def _reset_group_send_times():
    """The per-group rate limiter is module state — never let it leak."""
    from bob_server.services.whatsapp_outreach_tools import _group_send_times
    _group_send_times.clear()
    yield
    _group_send_times.clear()


@pytest.fixture
def stub_send():
    """Register a stub whatsapp_send executor (no bridge is constructed in
    tests), restoring whatever was registered before."""
    from bob_server.services import effects as effects_svc
    saved = effects_svc._EXECUTORS.get("whatsapp_send")
    stub = AsyncMock(return_value="req-fake")
    effects_svc.register_executor("whatsapp_send", stub)
    yield stub
    if saved is None:
        effects_svc._EXECUTORS.pop("whatsapp_send", None)
    else:
        effects_svc._EXECUTORS["whatsapp_send"] = saved


@pytest.fixture
def mock_wake(monkeypatch):
    wake = AsyncMock()
    monkeypatch.setattr("bob_server.services.wake_service.wake_conversation", wake)
    return wake


class _FakeBridge:
    connected = True


async def _seed(
    ctx, *, owner=True, group=True, requester=True,
) -> None:
    from bob_server.repositories.contacts import ContactRepository
    from bob_server.repositories.conversations import ConversationRepository

    if owner:
        await ctx.db.execute(
            "INSERT INTO contacts (id, name, phone_number, is_trusted, "
            "created_at, updated_at) VALUES ('c-mike', 'Mike Cleaver', "
            "'+61456224867', 1, datetime('now'), datetime('now'))")
        await ContactRepository(ctx.db).set_default("c-mike")
        await ConversationRepository(ctx.db).register_endpoint(
            OWNER_DM, endpoint_kind="dm", contact_id="c-mike")
    if requester:
        await ctx.db.execute(
            "INSERT INTO contacts (id, name, phone_number, is_trusted, "
            "created_at, updated_at) VALUES ('c-helen', 'Helen Burnside', "
            "'+61424193179', 1, datetime('now'), datetime('now'))")
    if group:
        await ConversationRepository(ctx.db).register_endpoint(
            GROUP_KEY, endpoint_kind="group", address=f"{GID}@g.us")
        await ctx.db.execute(
            "INSERT INTO whatsappgroups (id, whatsapp_jid, name, created_at, "
            "updated_at) VALUES ('g-1', ?, ?, datetime('now'), datetime('now'))",
            (f"{GID}@g.us", GROUP_NAME))


def _tools(ctx, *, requester_contact_id="c-helen", session=ORIGIN):
    from bob_server.services.whatsapp_outreach_tools import make_group_send_tools
    return {t.name: t for t in make_group_send_tools(
        ctx, _FakeBridge(), session, requester_contact_id=requester_contact_id)}


async def _request(ctx, message=MESSAGE, **kwargs) -> dict:
    tools = _tools(ctx, **kwargs)
    return json.loads(await tools["request_group_message"].handler(
        group_id=GID, message=message, note=""))


async def _approve(ctx, approval_id: str, decision: str = "approve") -> dict:
    from bob_server.services.approval_tools import make_approval_tools
    tools = {t.name: t for t in make_approval_tools(ctx, OWNER_DM)}
    return json.loads(await tools["respond_approval"].handler(
        approval_id=approval_id, decision=decision))


async def _sends(ctx) -> list[dict]:
    rows = await ctx.db.fetch_all(
        "SELECT * FROM effects WHERE kind = 'whatsapp_send' ORDER BY created_at")
    out = []
    for r in rows:
        item = dict(r)
        item["payload"] = json.loads(item["payload_json"] or "{}")
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Request: per-message approval routed to the owner's DM
# ---------------------------------------------------------------------------

async def test_request_creates_approval_and_wakes_owner_dm(ctx, db, mock_wake):
    from bob_server.repositories.conversations import ConversationRepository

    await _seed(ctx)
    await ConversationRepository(db).set_policy(GROUP_KEY, {"patience_enabled": 1})

    out = await _request(ctx)
    assert out["ok"] and out["sent"] is False, out
    approval_id = out["approval_id"]

    row = await ApprovalRepository(db).get(approval_id)
    assert row["approval_type"] == "group_send"
    assert row["entity_id"] == GROUP_KEY
    proposal = json.loads(row["proposal_data"])
    assert proposal["message"] == MESSAGE  # source of truth for delivery

    # Wake lands in the owner's DM, not the requesting group.
    mock_wake.assert_awaited_once()
    args = mock_wake.await_args
    assert args.args[1] == OWNER_DM
    assert args.args[1] != ORIGIN
    assert GROUP_NAME in args.args[2] and MESSAGE in args.args[2]
    assert "Helen" in args.args[2]  # requester identified via threaded contact

    # No standing consent: approving must never flip the group's policy.
    policy = await ConversationRepository(db).get_policy(GROUP_KEY)
    assert policy == {"patience_enabled": 1}


async def test_approve_emits_exactly_one_whatsapp_send(ctx, db, mock_wake, stub_send):
    await _seed(ctx)
    out = await _request(ctx)

    result = await _approve(ctx, out["approval_id"])
    assert result["ok"] and result["status"] == "approved"

    sends = await _sends(ctx)
    assert len(sends) == 1
    send = sends[0]
    assert send["idempotency_key"] == \
        f"whatsapp_group_send_approved:{out['approval_id']}"
    assert send["payload"]["chat_id"] == f"{GID}@g.us"
    assert send["payload"]["text"] == MESSAGE  # verbatim, from the stored row
    assert send["payload"]["origin_session_key"] == ORIGIN
    assert send["payload"]["approval_id"] == out["approval_id"]
    assert send["payload"]["group_send_approved"] is True

    # No other effect kinds beyond the approval pair and the send itself.
    rows = await db.fetch_all(
        "SELECT DISTINCT kind FROM effects WHERE kind NOT LIKE 'goal%' "
        "AND kind NOT IN ('approval_request', 'approval_respond', 'whatsapp_send')")
    assert rows == [], f"unexpected effects: {rows}"


async def test_approve_is_idempotent_on_respond_redelivery(ctx, db, mock_wake, stub_send):
    """C2 regression guard: a pump retry of approval_respond (already settled
    row) must re-run the follow-through without producing a second send."""
    from bob_server.services.effects import deliver

    await _seed(ctx)
    out = await _request(ctx)
    await _approve(ctx, out["approval_id"])
    assert len(await _sends(ctx)) == 1

    row = await db.fetch_one(
        "SELECT * FROM effects WHERE kind = 'approval_respond'")
    await deliver(ctx, dict(row))  # pump-style redelivery

    assert len(await _sends(ctx)) == 1  # keyed on approval_id — no duplicate
    assert stub_send.await_count == 1


async def test_reject_sends_nothing(ctx, db, mock_wake, stub_send):
    await _seed(ctx)
    out = await _request(ctx)

    result = await _approve(ctx, out["approval_id"], decision="reject")
    assert result["ok"] and result["status"] == "rejected"

    assert await _sends(ctx) == []
    stub_send.assert_not_awaited()


# ---------------------------------------------------------------------------
# Owner bypass — self-approval is pure friction
# ---------------------------------------------------------------------------

async def test_owner_contact_sends_directly(ctx, db, mock_wake, stub_send):
    await _seed(ctx)

    out = await _request(ctx, requester_contact_id="c-mike")
    assert out["ok"] and out["sent"] is True and out["chat_id"] == f"{GID}@g.us"

    assert await ApprovalRepository(db).pending() == []  # no approval row
    mock_wake.assert_not_awaited()

    sends = await _sends(ctx)
    assert len(sends) == 1
    assert sends[0]["payload"]["owner_direct"] is True
    assert sends[0]["payload"]["requested_by"] == "Mike Cleaver"


async def test_owner_dm_session_bypasses_without_contact_id(ctx, db, mock_wake, stub_send):
    """Wake-path group turns carry no contact id — the owner's own DM session
    must still bypass (C1 fallback)."""
    await _seed(ctx)

    out = await _request(ctx, requester_contact_id=None, session=OWNER_DM)
    assert out["ok"] and out["sent"] is True
    assert await ApprovalRepository(db).pending() == []


# ---------------------------------------------------------------------------
# Fail-closed and gate errors
# ---------------------------------------------------------------------------

async def test_fails_closed_without_default_contact(ctx, db, mock_wake):
    await _seed(ctx, owner=False, requester=True)

    out = await _request(ctx)
    assert out["ok"] is False and "default contact" in out["error"]

    assert await ApprovalRepository(db).pending() == []
    mock_wake.assert_not_awaited()
    assert await _sends(ctx) == []


async def test_membership_and_rate_limit_errors(ctx, db, mock_wake, stub_send):
    await _seed(ctx)

    tools = _tools(ctx)
    out = json.loads(await tools["request_group_message"].handler(
        group_id="999", message="hi", note=""))
    assert out["ok"] is False
    assert out["error"] == "Not a member of that group (no active group binding)"

    # Four owner sends exhaust the per-group budget; the fifth (from anyone)
    # is refused and creates no approval.
    for _ in range(4):
        out = await _request(ctx, requester_contact_id="c-mike")
        assert out["ok"] and out["sent"] is True, out
    out = await _request(ctx)
    assert out["ok"] is False and "rate limit" in out["error"]
    assert await ApprovalRepository(db).pending() == []


async def test_duplicate_pending_request_returns_existing(ctx, db, mock_wake):
    await _seed(ctx)

    first = await _request(ctx)
    second = await _request(ctx)

    assert second["ok"] and second["duplicate"] is True
    assert second["approval_id"] == first["approval_id"]
    pending = await ApprovalRepository(db).pending()
    assert len(pending) == 1
    mock_wake.assert_awaited_once()  # one wake, not two

    # A different message is a new request, not a duplicate.
    third = await _request(ctx, message="different text")
    assert third["ok"] and "approval_id" in third and "duplicate" not in third


async def test_membership_lost_before_approval_skips_send(ctx, db, mock_wake, stub_send):
    await _seed(ctx)
    out = await _request(ctx)

    await db.execute("UPDATE bindings SET is_active = 0 WHERE session_key = ?",
                     (GROUP_KEY,))

    result = await _approve(ctx, out["approval_id"])
    assert result["ok"] and result["status"] == "approved"
    assert await _sends(ctx) == []  # terminal skip — logged, not raised


# ---------------------------------------------------------------------------
# The policy-flag direct path is unchanged
# ---------------------------------------------------------------------------

async def test_direct_send_path_unchanged(ctx, db, mock_wake, stub_send):
    from bob_server.repositories.conversations import ConversationRepository

    await _seed(ctx)
    tools = _tools(ctx, requester_contact_id="c-mike")

    # Flag off → the existing policy error.
    out = json.loads(await tools["send_whatsapp_group_message"].handler(
        group_id=GID, message="proactive"))
    assert out["ok"] is False
    assert out["error"] == "Group outbound sends are not enabled for this group"

    # Flag on → same payload shape as before the refactor.
    await ConversationRepository(db).set_policy(
        GROUP_KEY, {"group_outbound_enabled": True})
    out = json.loads(await tools["send_whatsapp_group_message"].handler(
        group_id=GID, message="proactive", goal_id="goal-1"))
    assert out["ok"] and out["chat_id"] == f"{GID}@g.us"

    sends = await _sends(ctx)
    assert len(sends) == 1
    assert sends[0]["payload"]["goal_id"] == "goal-1"
    assert sends[0]["payload"]["origin_session_key"] == ORIGIN
    assert "approval_id" not in sends[0]["payload"]


async def test_list_pending_approvals_includes_group_fields(ctx, db, mock_wake):
    from bob_server.services.approval_tools import make_approval_tools

    await _seed(ctx)
    await _request(ctx)

    tools = {t.name: t for t in make_approval_tools(ctx, OWNER_DM)}
    out = json.loads(await tools["list_pending_approvals"].handler())
    assert out["ok"] and len(out["pending"]) == 1
    item = out["pending"][0]
    assert item["group_name"] == GROUP_NAME
    assert item["group_id"] == GID
    assert item["message"] == MESSAGE
