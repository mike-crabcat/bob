"""Steering — requests that wake a target conversation (docs/steering-plan.md).

Membership falls out of target resolution (candidates come only from the
requester's own conversations); the owner steers directly, everyone else's
request routes to the owner's DM as a ``conversation_steer`` approval whose
proposal stores the rendered wake verbatim — approving fires exactly that
wake once, through an idempotency-keyed ``conversation_steer`` effect.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

# Importing approval_tools registers the approval effect executors and (via
# its registration tail) steering's conversation_steer executor + on-approved
# hook — in production the bridge's tool assembly guarantees this before any
# request can run.
from bob_server.services import approval_tools  # noqa: F401
from bob_server.services import steering

from bob_server.repositories.approvals import ApprovalRepository

OWNER_DM = "agent:main:whatsapp:dm:61456224867"
HELEN_DM = "agent:main:whatsapp:dm:61424193179"
GID = "120363401238199025"
GROUP_KEY = f"agent:main:whatsapp:group:{GID}"
GROUP_NAME = "Leeming Boys"
GID2 = "120363408889690088"
ORIGIN = f"agent:main:whatsapp:group:{GID2}"
ORIGIN_NAME = "Pirate Radio"
INSTRUCTION = "Let them know the radio feature set is on — pull the promo notes from the workspace"


@pytest.fixture(autouse=True)
def _ensure_registered():
    """executors_reset_for_tests (other suites) may have cleared the
    registry after this module's import-time registration ran."""
    steering.register()
    yield


@pytest.fixture
def mock_wake(monkeypatch):
    wake = AsyncMock()
    monkeypatch.setattr("bob_server.services.wake_service.wake_conversation", wake)
    return wake


@pytest.fixture
def stub_send():
    """Stub whatsapp_send executor for the kept proactive-send tool (no
    bridge is constructed in tests), restoring whatever was registered."""
    from bob_server.services import effects as effects_svc
    saved = effects_svc._EXECUTORS.get("whatsapp_send")
    stub = AsyncMock(return_value="req-fake")
    effects_svc.register_executor("whatsapp_send", stub)
    yield stub
    if saved is None:
        effects_svc._EXECUTORS.pop("whatsapp_send", None)
    else:
        effects_svc._EXECUTORS["whatsapp_send"] = saved


async def _group(db, gid: str, name: str, *member_ids: str) -> None:
    from bob_server.repositories.conversations import ConversationRepository

    await ConversationRepository(db).register_endpoint(
        f"agent:main:whatsapp:group:{gid}", endpoint_kind="group",
        address=f"{gid}@g.us")
    await db.execute(
        "INSERT INTO whatsappgroups (id, whatsapp_jid, name, created_at, "
        "updated_at) VALUES (?, ?, ?, datetime('now'), datetime('now'))",
        (f"g-{gid}", f"{gid}@g.us", name))
    for contact_id in member_ids:
        await db.execute(
            "INSERT INTO whatsappgroup_members (id, group_id, contact_id, "
            "display_name, joined_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), datetime('now'))",
            (f"m-{gid}-{contact_id}", f"g-{gid}", contact_id, contact_id))


async def _seed(ctx, *, owner=True) -> None:
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
    await ctx.db.execute(
        "INSERT INTO contacts (id, name, phone_number, is_trusted, "
        "created_at, updated_at) VALUES ('c-helen', 'Helen Burnside', "
        "'+61424193179', 1, datetime('now'), datetime('now'))")
    await ConversationRepository(ctx.db).register_endpoint(
        HELEN_DM, endpoint_kind="dm", contact_id="c-helen")
    # Leeming Boys: both are members; Pirate Radio: Helen only (the origin).
    leeming = ("c-mike", "c-helen") if owner else ("c-helen",)
    await _group(ctx.db, GID, GROUP_NAME, *leeming)
    await _group(ctx.db, GID2, ORIGIN_NAME, "c-helen")


def _tools(ctx, *, requester_contact_id="c-helen", session=ORIGIN):
    return {t.name: t for t in steering.make_steering_tools(
        ctx, session, requester_contact_id)}


async def _steer(ctx, target=GROUP_NAME, instruction=INSTRUCTION, **kwargs) -> dict:
    tools = _tools(ctx, **kwargs)
    return json.loads(await tools["steer_conversation"].handler(
        target=target, instruction=instruction))


async def _approve(ctx, approval_id: str, decision: str = "approve") -> dict:
    tools = {t.name: t for t in approval_tools.make_approval_tools(ctx, OWNER_DM)}
    return json.loads(await tools["respond_approval"].handler(
        approval_id=approval_id, decision=decision))


async def _steer_effects(ctx) -> list[dict]:
    rows = await ctx.db.fetch_all(
        "SELECT * FROM effects WHERE kind = 'conversation_steer' ORDER BY created_at")
    out = []
    for r in rows:
        item = dict(r)
        item["payload"] = json.loads(item["payload_json"] or "{}")
        out.append(item)
    return out


def _wakes_to(mock_wake, session_key: str) -> list:
    return [c for c in mock_wake.call_args_list if c.args[1] == session_key]


# ---------------------------------------------------------------------------
# Resolution — membership is the resolution
# ---------------------------------------------------------------------------

async def test_resolves_group_by_name_id_and_own_dm(ctx, db, mock_wake):
    await _seed(ctx)

    out = await _steer(ctx)
    assert out["ok"] and out["steered"] is False, out  # non-owner → approval
    assert (await ApprovalRepository(db).pending())[0]["entity_id"] == GROUP_KEY

    out = await _steer(ctx, target=GID)  # raw id, never fuzzy-matched
    assert out["ok"] and out["steered"] is False, out

    out = await _steer(ctx, target="my chat")  # own DM
    assert out["ok"], out
    row = await ApprovalRepository(db).pending_of_type(
        "conversation_steer", entity_id=HELEN_DM)
    assert row, "own-DM steer from a non-owner still routes through approval"


async def test_non_member_group_never_resolves(ctx, db, mock_wake):
    await _seed(ctx)

    out = await _steer(ctx, target="ai doom")
    assert out["ok"] is False
    assert "No group matching" in out["error"]
    # Candidates ride the error so the requesting turn can ask, not guess.
    assert {c["name"] for c in out["candidates"]} == {GROUP_NAME, ORIGIN_NAME}
    assert await ApprovalRepository(db).pending() == []
    mock_wake.assert_not_awaited()


async def test_ambiguous_name_returns_candidates(ctx, db, mock_wake):
    await _seed(ctx)
    await _group(db, "120363409999999999", "Leeming Boys BBQ", "c-helen")

    out = await _steer(ctx, target="leeming")
    assert out["ok"] is False and "Multiple groups" in out["error"]
    assert len(out["candidates"]) == 2
    assert await ApprovalRepository(db).pending() == []


async def test_lost_group_binding_fails_closed(ctx, db, mock_wake):
    await _seed(ctx)
    await db.execute("UPDATE bindings SET is_active = 0 WHERE session_key = ?",
                     (GROUP_KEY,))

    out = await _steer(ctx)
    assert out["ok"] is False and "no longer active" in out["error"]


async def test_no_contact_record_fails_closed(ctx, db, mock_wake):
    await _seed(ctx)
    tools = _tools(ctx, requester_contact_id=None)

    out = json.loads(await tools["steer_conversation"].handler(
        target=GROUP_NAME, instruction=INSTRUCTION))
    assert out["ok"] is False and "No contact record" in out["error"]


# ---------------------------------------------------------------------------
# Owner bypass — self-approval is pure friction
# ---------------------------------------------------------------------------

async def test_owner_steers_directly(ctx, db, mock_wake):
    await _seed(ctx)

    out = await _steer(ctx, requester_contact_id="c-mike")
    assert out["ok"] and out["steered"] is True and out["target"] == GROUP_NAME
    assert await ApprovalRepository(db).pending() == []

    wakes = _wakes_to(mock_wake, GROUP_KEY)
    assert len(wakes) == 1
    args, kwargs = wakes[0]
    assert INSTRUCTION in args[2]
    assert "Mike Cleaver" in args[2]  # attributed in the stored header
    assert f"via the {ORIGIN_NAME} group" in args[2]
    assert kwargs["provenance"] == "steer"
    assert kwargs["metadata"]["owner_direct"] is True
    assert kwargs["metadata"]["requester_contact_id"] == "c-mike"


async def test_owner_dm_origin_bypasses_without_contact_id(ctx, db, mock_wake):
    """The wake-path fallback: an owner-DM origin bypasses even when no
    contact was threaded in (resolution is tool-side; create_request owns
    the bypass)."""
    await _seed(ctx)

    out = await steering.create_request(
        ctx, target_key=GROUP_KEY, target_label=GROUP_NAME,
        instruction=INSTRUCTION, origin_session_key=OWNER_DM,
        origin_label="a test", requester_contact_id=None)
    assert out["ok"] and out["steered"] is True
    assert await ApprovalRepository(db).pending() == []
    assert len(_wakes_to(mock_wake, GROUP_KEY)) == 1


# ---------------------------------------------------------------------------
# Non-owner requests — approval-gated
# ---------------------------------------------------------------------------

async def test_request_creates_approval_and_wakes_owner_dm(ctx, db, mock_wake):
    await _seed(ctx)

    out = await _steer(ctx)
    assert out["ok"] and out["steered"] is False and out["approval_id"], out

    row = await ApprovalRepository(db).get(out["approval_id"])
    assert row["approval_type"] == "conversation_steer"
    assert row["entity_id"] == GROUP_KEY
    proposal = json.loads(row["proposal_data"])
    assert proposal["instruction"] == INSTRUCTION
    assert proposal["content"].startswith("[Steering request — Helen Burnside")
    assert INSTRUCTION in proposal["content"]

    # One wake, to the owner's DM, carrying target + requester + instruction.
    mock_wake.assert_awaited_once()
    args = mock_wake.await_args
    assert args.args[1] == OWNER_DM
    for expected in (GROUP_NAME, "Helen Burnside", INSTRUCTION):
        assert expected in args.args[2], args.args[2]
    assert not _wakes_to(mock_wake, GROUP_KEY)


async def test_approve_fires_exactly_one_wake_with_steer_provenance(ctx, db, mock_wake):
    await _seed(ctx)
    out = await _steer(ctx)
    mock_wake.reset_mock()

    result = await _approve(ctx, out["approval_id"])
    assert result["ok"] and result["status"] == "approved"

    effects = await _steer_effects(ctx)
    assert len(effects) == 1
    assert effects[0]["idempotency_key"] == \
        f"steer_wake_approved:{out['approval_id']}"
    payload = effects[0]["payload"]
    assert payload["target_key"] == GROUP_KEY
    assert INSTRUCTION in payload["content"]  # verbatim from the proposal

    wakes = _wakes_to(mock_wake, GROUP_KEY)
    assert len(wakes) == 1
    kwargs = wakes[0].kwargs
    assert kwargs["provenance"] == "steer"
    assert kwargs["call_category"] == "steer"
    meta = kwargs["metadata"]
    assert meta["steer"] is True
    assert meta["requester_name"] == "Helen Burnside"
    assert meta["requester_contact_id"] == "c-helen"
    assert meta["origin_session_key"] == ORIGIN
    assert meta["approval_id"] == out["approval_id"]


async def test_approve_is_idempotent_on_respond_redelivery(ctx, db, mock_wake):
    """A pump retry of approval_respond (already settled row) must re-run the
    follow-through without producing a second wake."""
    from bob_server.services.effects import deliver

    await _seed(ctx)
    out = await _steer(ctx)
    await _approve(ctx, out["approval_id"])
    assert len(_wakes_to(mock_wake, GROUP_KEY)) == 1

    row = await db.fetch_one(
        "SELECT * FROM effects WHERE kind = 'approval_respond'")
    await deliver(ctx, dict(row))  # pump-style redelivery

    assert len(_wakes_to(mock_wake, GROUP_KEY)) == 1  # keyed on approval_id
    assert len(await _steer_effects(ctx)) == 1


async def test_reject_wakes_nothing(ctx, db, mock_wake):
    await _seed(ctx)
    out = await _steer(ctx)
    mock_wake.reset_mock()

    result = await _approve(ctx, out["approval_id"], decision="reject")
    assert result["ok"] and result["status"] == "rejected"

    assert await _steer_effects(ctx) == []
    mock_wake.assert_not_awaited()


async def test_duplicate_pending_request_returns_existing(ctx, db, mock_wake):
    await _seed(ctx)

    first = await _steer(ctx)
    second = await _steer(ctx)
    assert second["ok"] and second["duplicate"] is True
    assert second["approval_id"] == first["approval_id"]
    assert len(await ApprovalRepository(db).pending()) == 1
    mock_wake.assert_awaited_once()  # one approval wake, not two

    third = await _steer(ctx, instruction="different intent entirely")
    assert third["ok"] and "duplicate" not in third


async def test_binding_lost_before_approval_skips_wake(ctx, db, mock_wake):
    await _seed(ctx)
    out = await _steer(ctx)
    mock_wake.reset_mock()

    await db.execute("UPDATE bindings SET is_active = 0 WHERE session_key = ?",
                     (GROUP_KEY,))
    result = await _approve(ctx, out["approval_id"])
    assert result["ok"] and result["status"] == "approved"
    assert await _steer_effects(ctx) == []  # terminal skip — logged, not raised
    mock_wake.assert_not_awaited()


async def test_fails_closed_without_default_contact(ctx, db, mock_wake):
    await _seed(ctx, owner=False)

    out = await _steer(ctx)
    assert out["ok"] is False and "default contact" in out["error"]
    assert await ApprovalRepository(db).pending() == []
    mock_wake.assert_not_awaited()


async def test_list_pending_approvals_includes_steer_fields(ctx, db, mock_wake):
    await _seed(ctx)
    await _steer(ctx)

    tools = {t.name: t for t in approval_tools.make_approval_tools(ctx, OWNER_DM)}
    out = json.loads(await tools["list_pending_approvals"].handler())
    assert out["ok"] and len(out["pending"]) == 1
    item = out["pending"][0]
    assert item["target"] == GROUP_NAME
    assert item["instruction"] == INSTRUCTION


# ---------------------------------------------------------------------------
# Rendering — a steer never reads as someone speaking in the target
# ---------------------------------------------------------------------------

async def test_prompt_replay_labels_steer_rows(ctx, db):
    from bob_server.services.prompt_assembler import build_chat_messages
    from bob_server.services.session_service import SessionService

    old = await SessionService(ctx).add_message(
        GROUP_KEY, "user", "[Steering request — Helen Burnside, via the Pirate Radio group]\nold ask",
        channel="whatsapp", provenance="steer", dispatched=1)
    new = await SessionService(ctx).add_message(
        GROUP_KEY, "user", "[Steering request — Helen Burnside, via the Pirate Radio group]\nnew ask",
        channel="whatsapp", provenance="steer", dispatched=0)

    messages = await build_chat_messages(
        session_key=GROUP_KEY, db=db, claimed_ids={new}, max_history=10)
    rendered = "\n".join(
        m["content"] for m in messages if isinstance(m.get("content"), str))

    # Historical and claimed steer rows both carry the system-relay marker;
    # the claimed one is additionally the turn's [NEW] stimulus.
    assert rendered.count("[system relay — steering request]") == 2
    assert "[NEW — awaiting your reply]" in rendered
    assert isinstance(old, str)  # message ids are strings


# ---------------------------------------------------------------------------
# Retirement — the verbatim relay is gone, the proactive send stays
# ---------------------------------------------------------------------------

async def test_proactive_group_send_kept_and_relay_retired(ctx, db, mock_wake, stub_send):
    from bob_server.repositories.conversations import ConversationRepository
    from bob_server.services.whatsapp_outreach_tools import make_group_send_tools

    class _FakeBridge:
        connected = True

    await _seed(ctx)
    tools = make_group_send_tools(ctx, _FakeBridge(), ORIGIN)
    assert [t.name for t in tools] == ["send_whatsapp_group_message"]

    # Policy gate unchanged: off by default.
    out = json.loads(await tools[0].handler(group_id=GID, message="proactive"))
    assert out["ok"] is False
    assert out["error"] == "Group outbound sends are not enabled for this group"

    await ConversationRepository(db).set_policy(
        GROUP_KEY, {"group_outbound_enabled": True})
    out = json.loads(await tools[0].handler(
        group_id=GID, message="proactive", goal_id="goal-1"))
    assert out["ok"] and out["chat_id"] == f"{GID}@g.us"
