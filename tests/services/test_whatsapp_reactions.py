"""WhatsApp emoji reactions — inbound storage, rendering, wake (2026-09-14).

Reactions land as self-describing user rows (provenance wa_reaction):
passive by default (dispatched=1, never awaiting-reply), rendered inline in
replayed history. A DM reaction that targets one of Bob's own messages
(resolvable via the assistant row's stamped WhatsApp id) wakes a turn;
group reactions and reactions to user messages never wake.

Pinned here at the ingestion seam (storage shape, dedupe, drop gates, the
wake matrix) and the rendering seam (passive line, group sender prefix,
claimed [NEW] trailer line).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from server.services.prompt_assembler import build_chat_messages
from server.services.session_service import SessionService

from tests.services.test_whatsapp_inbound_characterization import (
    GROUP_JID,
    TRUSTED_PHONE,
    _group_payload,
    _make_service,
    _seed_contact,
)

DM_KEY = f"agent:main:whatsapp:dm:{TRUSTED_PHONE.lstrip('+')}"
GROUP_KEY = f"agent:main:whatsapp:group:{GROUP_JID.split('@')[0]}"


def _reaction_payload(*, chat_kind: str = "dm", sender_phone: str = TRUSTED_PHONE,
                      target: str = "wamid-target-1", emoji: str = "👍",
                      rid: str = "wamid-react-1") -> dict:
    if chat_kind == "group":
        chat_id = GROUP_JID
    else:
        chat_id = f"{sender_phone.lstrip('+')}@s.whatsapp.net"
    return {
        "chat_id": chat_id,
        "chat_kind": chat_kind,
        "sender_jid": f"{sender_phone.lstrip('+')}@s.whatsapp.net",
        "sender_name": "Tester",
        "target_message_id": target,
        "emoji": emoji,
        "whatsapp_message_id": rid,
    }


class AckWS:
    """FakeWS that captures ack frames."""

    def __init__(self) -> None:
        self.acks: list[str] = []

    async def send(self, raw: str) -> None:
        frame = json.loads(raw)
        if frame.get("type") == "ack":
            self.acks.append(frame["payload"]["message_id"])


@pytest.fixture(autouse=True)
def outbound_flag_on(ctx):
    """This box's rollout gate (BOB_WHATSAPP_BRIDGE_OUTBOUND_REACTIONS=off in
    ~/config/.env) leaks into Settings.from_env() — force the flag on so the
    suite doesn't depend on the host's rollout state. Tests that need it off
    set it off themselves."""
    ctx.settings.whatsapp_bridge.outbound_reactions_enabled = True
    yield
    ctx.settings.whatsapp_bridge.outbound_reactions_enabled = True


def _svc(ctx, tmp_path) -> tuple:
    svc = _make_service(ctx, tmp_path)
    # _make_service builds its own Settings via from_env() — same rollout
    # leak as the autouse fixture above.
    svc._get_settings().whatsapp_bridge.outbound_reactions_enabled = True
    ws = AckWS()
    svc._ws = ws
    svc._dispatch_reaction_turn = AsyncMock()  # type: ignore[method-assign]
    return svc, ws


async def _seed_user_row(ctx, text: str, wa_id: str, *, key: str = DM_KEY) -> None:
    """User row with its WhatsApp id. Caller seeds the contact first — the
    phone number is UNIQUE, so double-seeding raises."""
    from server.repositories.contacts import ContactRepository
    contact = await ContactRepository(ctx.db).get_by_phone(TRUSTED_PHONE)
    await SessionService(ctx).add_message(
        key, "user", text, dispatched=1, channel="whatsapp",
        sender_id=contact["id"] if contact else None,
        metadata={"wa_message_id": wa_id})


async def _seed_bob_row(ctx, text: str, wa_id: str, *, key: str = DM_KEY) -> None:
    await SessionService(ctx).add_message(
        key, "assistant", text, dispatched=1, channel="whatsapp",
        metadata={"sends": [{"request_id": "req-1", "wa_message_id": wa_id}]})


async def _reaction_rows(db, like: str) -> list:
    return await db.fetch_all(
        "SELECT * FROM messages WHERE conversation_id LIKE ? "
        "AND provenance = 'wa_reaction' ORDER BY rowid", (like,))


# ------------------------------------------------------------ ingestion


@pytest.mark.asyncio
async def test_dm_reaction_to_user_message_is_passive(ctx, tmp_path):
    await _seed_contact(ctx.db, TRUSTED_PHONE)
    await _seed_user_row(ctx, "see you at 6", "wamid-target-1")
    svc, ws = _svc(ctx, tmp_path)

    await svc._handle_incoming_reaction(_reaction_payload())

    rows = await _reaction_rows(ctx.db, f"{DM_KEY}%")
    assert len(rows) == 1
    row = rows[0]
    assert row["role"] == "user"
    assert row["dispatched"] == 1
    assert row["content"] == '[reaction 👍 to: "see you at 6"]'
    meta = json.loads(row["metadata"])
    assert meta["reaction"]["target_wa_message_id"] == "wamid-target-1"
    assert meta["reaction"]["target_role"] == "user"
    assert meta["wa_message_id"] == "wamid-react-1"
    # No wake, acked.
    svc._dispatch_reaction_turn.assert_not_awaited()
    assert ws.acks == ["wamid-react-1"]


@pytest.mark.asyncio
async def test_dm_reaction_to_bob_message_wakes(ctx, tmp_path):
    await _seed_contact(ctx.db, TRUSTED_PHONE)
    await _seed_bob_row(ctx, "great, see you then!", "wamid-target-1")
    svc, ws = _svc(ctx, tmp_path)

    await svc._handle_incoming_reaction(_reaction_payload())

    rows = await _reaction_rows(ctx.db, f"{DM_KEY}%")
    assert len(rows) == 1
    assert rows[0]["dispatched"] == 0
    assert rows[0]["content"] == (
        '[reaction 👍 to your message: "great, see you then!"]')
    svc._dispatch_reaction_turn.assert_awaited_once()
    kwargs = svc._dispatch_reaction_turn.await_args.kwargs
    assert kwargs["session_key"].startswith(DM_KEY)


@pytest.mark.asyncio
async def test_group_reaction_to_bob_message_stays_passive(ctx, tmp_path):
    await _seed_contact(ctx.db, TRUSTED_PHONE)
    await _seed_bob_row(ctx, "my joke", "wamid-target-1", key=GROUP_KEY)
    svc, ws = _svc(ctx, tmp_path)

    await svc._handle_incoming_reaction(_reaction_payload(chat_kind="group"))

    rows = await _reaction_rows(ctx.db, f"{GROUP_KEY}%")
    assert len(rows) == 1 and rows[0]["dispatched"] == 1
    svc._dispatch_reaction_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_unresolvable_target_stays_passive(ctx, tmp_path):
    await _seed_contact(ctx.db, TRUSTED_PHONE)
    svc, ws = _svc(ctx, tmp_path)

    await svc._handle_incoming_reaction(_reaction_payload(target="wamid-ghost"))

    rows = await _reaction_rows(ctx.db, f"{DM_KEY}%")
    assert len(rows) == 1 and rows[0]["dispatched"] == 1
    assert rows[0]["content"] == "[reaction 👍 to an earlier message]"
    svc._dispatch_reaction_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_removal_emoji_renders_removed(ctx, tmp_path):
    await _seed_contact(ctx.db, TRUSTED_PHONE)
    await _seed_user_row(ctx, "see you at 6", "wamid-target-1")
    svc, ws = _svc(ctx, tmp_path)

    await svc._handle_incoming_reaction(_reaction_payload(emoji=""))

    rows = await _reaction_rows(ctx.db, f"{DM_KEY}%")
    assert rows[0]["content"] == '[reaction removed from: "see you at 6"]'


@pytest.mark.asyncio
async def test_bridge_redelivery_stores_exactly_one_row(ctx, tmp_path):
    await _seed_contact(ctx.db, TRUSTED_PHONE)
    await _seed_user_row(ctx, "hi", "wamid-target-1")
    svc, ws = _svc(ctx, tmp_path)

    payload = _reaction_payload()
    await svc._handle_incoming_reaction(payload)
    await svc._handle_incoming_reaction(payload)  # bridge redelivery

    rows = await _reaction_rows(ctx.db, f"{DM_KEY}%")
    assert len(rows) == 1
    # Both deliveries acked (queue rows cleared either way).
    assert ws.acks == ["wamid-react-1", "wamid-react-1"]


@pytest.mark.asyncio
async def test_kill_switch_acks_but_stores_nothing(ctx, tmp_path):
    await _seed_contact(ctx.db, TRUSTED_PHONE)
    svc, ws = _svc(ctx, tmp_path)
    svc._get_settings().whatsapp_bridge.inbound_reactions_enabled = False

    await svc._handle_incoming_reaction(_reaction_payload())

    assert await _reaction_rows(ctx.db, "%") == []
    svc._dispatch_reaction_turn.assert_not_awaited()
    assert ws.acks == ["wamid-react-1"]


@pytest.mark.asyncio
async def test_unknown_dm_number_dropped_but_acked(ctx, tmp_path):
    # No contact seeded → unknown number.
    svc, ws = _svc(ctx, tmp_path)

    await svc._handle_incoming_reaction(_reaction_payload(
        sender_phone="+614999900999"))

    assert await _reaction_rows(ctx.db, "%") == []
    assert ws.acks == ["wamid-react-1"]


# ------------------------------------------------------------ rendering


async def _build(ctx, key: str, **kwargs):
    return await build_chat_messages(
        None, key, db=ctx.db, system_content="sys", max_history=50, **kwargs)


async def test_passive_reaction_renders_inline(ctx, tmp_path, db):
    await _seed_contact(ctx.db, TRUSTED_PHONE, name="Mike T")
    await _seed_user_row(ctx, "see you at 6", "wamid-target-1")
    await SessionService(ctx).add_message(
        DM_KEY, "user", '[reaction 👍 to: "see you at 6"]',
        dispatched=1, channel="whatsapp", provenance="wa_reaction")

    messages = await _build(ctx, DM_KEY)
    user_lines = [m["content"] for m in messages
                  if m.get("role") == "user" and isinstance(m.get("content"), str)]
    assert '[reaction 👍 to: "see you at 6"]' in user_lines


async def test_group_reaction_line(ctx, db):
    contact_id = await _seed_contact(ctx.db, TRUSTED_PHONE, name="Mike T")
    from server.repositories.participants import ParticipantRepository
    await ParticipantRepository(db).upsert(
        GROUP_KEY, TRUSTED_PHONE, display_name="Mike T",
        contact_id=contact_id, is_trusted=True,
        now_iso="2026-09-14T00:00:00Z")
    await SessionService(ctx).add_message(
        GROUP_KEY, "user", '[reaction 👍 to: "the plan"]',
        dispatched=1, channel="whatsapp", sender_id=contact_id,
        provenance="wa_reaction")

    messages = await _build(ctx, GROUP_KEY)
    user_lines = [m["content"] for m in messages
                  if m.get("role") == "user" and isinstance(m.get("content"), str)]
    assert any(line.startswith("[Mike T]")
               and '[reaction 👍 to: "the plan"]' in line
               for line in user_lines), user_lines


async def test_wake_reaction_row_claims_with_new_marker(ctx, db):
    contact_id = await _seed_contact(ctx.db, TRUSTED_PHONE, name="Mike T")
    await SessionService(ctx).add_message(
        DM_KEY, "user", '[reaction ❤️ to your message: "sounds good"]',
        dispatched=0, channel="whatsapp", sender_id=contact_id,
        provenance="wa_reaction")

    from server.repositories.history import HistoryRepository
    claimed = await HistoryRepository(db).pending_user_ids(DM_KEY)
    messages = await _build(ctx, DM_KEY, claimed_ids=set(claimed))
    rendered = "\n".join(
        m["content"] for m in messages if isinstance(m.get("content"), str))
    assert "[NEW" in rendered or "New message" in rendered


# ------------------------------------------------------------ runner gate


def test_wa_reaction_is_silence_ok():
    """A reaction-only wake turn must not be rescued into sending: the
    designed outcome is NO_REPLY."""
    from server.services.dispatch_runner import _SILENCE_OK_PROVENANCES
    assert "wa_reaction" in _SILENCE_OK_PROVENANCES


# ------------------------------------------------------------ outbound


class FrameWS:
    """FakeWS that captures send_reaction frames and acks."""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def send(self, raw: str) -> None:
        self.frames.append(json.loads(raw))


def _capture_emitter(monkeypatch):
    calls: list[dict] = []

    async def _fake(ctx, *, kind, idempotency_key, payload):
        calls.append({"kind": kind, "key": idempotency_key, "payload": payload})
        return {"ok": True, "external_result_id": "req-r1"}

    monkeypatch.setattr("server.services.effects.emit_and_deliver", _fake)
    return calls


def _tool_svc(ctx, tmp_path):
    """_make_service with the outbound flag forced on (rollout-leak guard,
    see the autouse fixture)."""
    svc = _make_service(ctx, tmp_path)
    svc._get_settings().whatsapp_bridge.outbound_reactions_enabled = True
    return svc


def _react_tool(svc, ctx, *, chat_kind: str = "dm", seq=None):
    from server.services.whatsapp_bridge_service._reactions import make_react_tool
    chat_id = GROUP_JID if chat_kind == "group" else f"{TRUSTED_PHONE.lstrip('+')}@s.whatsapp.net"
    return make_react_tool(ctx, svc, DM_KEY if chat_kind == "dm" else GROUP_KEY,
                           chat_id, chat_kind, "dispatch-1", seq or [0])


async def test_react_tool_sends_reaction_effect_and_records_row(
        ctx, tmp_path, monkeypatch):
    contact_id = await _seed_contact(ctx.db, TRUSTED_PHONE)
    await SessionService(ctx).add_message(
        DM_KEY, "user", "sounds good", dispatched=1, channel="whatsapp",
        sender_id=contact_id, metadata={"wa_message_id": "wamid-t1"})
    svc = _tool_svc(ctx, tmp_path)
    calls = _capture_emitter(monkeypatch)
    tool = _react_tool(svc, ctx)

    result = await tool.handler(emoji="👍")

    assert result.startswith("Reacted 👍")
    assert len(calls) == 1
    assert calls[0]["kind"] == "whatsapp_react"
    assert calls[0]["key"] == "whatsapp_react:dispatch-1:0"
    assert calls[0]["payload"] == {
        "chat_id": f"{TRUSTED_PHONE.lstrip('+')}@s.whatsapp.net",
        "target_message_id": "wamid-t1",
        "target_sender_jid": f"{TRUSTED_PHONE.lstrip('+')}@s.whatsapp.net",
        "emoji": "👍",
    }
    # Sequence counter shared with the send tool: next send would be seq 1.
    rows = await ctx.db.fetch_all(
        "SELECT * FROM messages WHERE provenance = 'wa_reaction_sent'")
    assert len(rows) == 1 and rows[0]["role"] == "assistant"
    assert rows[0]["content"] == '[you reacted 👍 to: "sounds good"]'


async def test_react_tool_own_last_message_targets_bob_row(ctx, tmp_path, monkeypatch):
    await _seed_contact(ctx.db, TRUSTED_PHONE)
    await _seed_bob_row(ctx, "my message", "wamid-b1")
    svc = _tool_svc(ctx, tmp_path)
    calls = _capture_emitter(monkeypatch)
    tool = _react_tool(svc, ctx)

    await tool.handler(emoji="❤️", target="own_last_message")

    assert calls[0]["payload"]["target_message_id"] == "wamid-b1"
    # Empty author JID → whatsmeow FromMe key (reacting to his own message).
    assert calls[0]["payload"]["target_sender_jid"] == ""


async def test_react_tool_rejects_off_allowlist(ctx, tmp_path, monkeypatch):
    svc = _tool_svc(ctx, tmp_path)
    calls = _capture_emitter(monkeypatch)
    tool = _react_tool(svc, ctx)

    for bad in ("💩", "👍👍", "", "thumbs up"):
        result = await tool.handler(emoji=bad)
        assert result.startswith("Error: not sent"), bad
    assert calls == []


async def test_react_tool_rejects_bad_target_enum(ctx, tmp_path, monkeypatch):
    svc = _tool_svc(ctx, tmp_path)
    calls = _capture_emitter(monkeypatch)
    tool = _react_tool(svc, ctx)

    result = await tool.handler(emoji="👍", target="message_42")
    assert result.startswith("Error: not sent")
    assert calls == []


async def test_react_tool_unresolvable_target_errors(ctx, tmp_path, monkeypatch):
    await _seed_contact(ctx.db, TRUSTED_PHONE)
    svc = _tool_svc(ctx, tmp_path)
    calls = _capture_emitter(monkeypatch)
    tool = _react_tool(svc, ctx)

    result = await tool.handler(emoji="👍")
    assert result.startswith("Error: no recent inbound message")
    assert calls == []


async def test_react_tool_duplicate_rejected_within_turn(ctx, tmp_path, monkeypatch):
    contact_id = await _seed_contact(ctx.db, TRUSTED_PHONE)
    await SessionService(ctx).add_message(
        DM_KEY, "user", "ok", dispatched=1, channel="whatsapp",
        sender_id=contact_id, metadata={"wa_message_id": "wamid-t1"})
    svc = _tool_svc(ctx, tmp_path)
    calls = _capture_emitter(monkeypatch)
    tool = _react_tool(svc, ctx)

    await tool.handler(emoji="👍")
    result = await tool.handler(emoji="👍")  # same target + emoji again
    assert result.startswith("Error: not sent — you already reacted")
    assert len(calls) == 1


async def test_react_tool_kill_switch(ctx, tmp_path, monkeypatch):
    svc = _tool_svc(ctx, tmp_path)
    svc._get_settings().whatsapp_bridge.outbound_reactions_enabled = False
    calls = _capture_emitter(monkeypatch)
    tool = _react_tool(svc, ctx)

    result = await tool.handler(emoji="👍")
    assert result == "Error: reactions are disabled."
    assert calls == []


async def test_react_tool_group_target_resolves_participant_jid(
        ctx, tmp_path, monkeypatch):
    contact_id = await _seed_contact(ctx.db, TRUSTED_PHONE, name="Mike T")
    await SessionService(ctx).add_message(
        GROUP_KEY, "user", "the plan", dispatched=1, channel="whatsapp",
        sender_id=contact_id, metadata={"wa_message_id": "wamid-g1"})
    svc = _tool_svc(ctx, tmp_path)
    calls = _capture_emitter(monkeypatch)
    tool = _react_tool(svc, ctx, chat_kind="group")

    result = await tool.handler(emoji="👍")

    assert result.startswith("Reacted 👍")
    assert calls[0]["payload"]["target_sender_jid"] == (
        f"{TRUSTED_PHONE.lstrip('+')}@s.whatsapp.net")


async def test_send_reaction_frame_shape(tmp_path, ctx):
    svc = _tool_svc(ctx, tmp_path)
    ws = FrameWS()
    svc._ws = ws

    rid = await svc.send_reaction(GROUP_JID, "wamid-t1", "", "👍")

    assert len(ws.frames) == 1
    frame = ws.frames[0]
    assert frame["type"] == "send_reaction"
    assert frame["payload"] == {
        "chat_id": GROUP_JID,
        "target_message_id": "wamid-t1",
        "target_sender_jid": "",
        "emoji": "👍",
        "request_id": rid,
    }


async def test_react_executor_registered():
    from server.services import effects as effects_svc
    assert "whatsapp_react" in effects_svc._EXECUTORS


# ------------------------------------------------------------ tool attach


async def _spec(ctx, **overrides):
    from server.services.whatsapp_bridge_service._service import WhatsAppBridgeService
    kwargs = dict(
        session_key=DM_KEY,
        chat_id=f"{TRUSTED_PHONE.lstrip('+')}@s.whatsapp.net",
        chat_kind="dm", contact_id=None, is_trusted=True, human_initiated=True)
    kwargs.update(overrides)
    return await WhatsAppBridgeService(ctx)._build_inbound_dispatch_spec(**kwargs)


async def test_react_tool_attached_by_default(ctx, tmp_path):
    spec = await _spec(ctx)
    names = {t.name for t in spec.tools}
    assert "react_whatsapp_message" in names


async def test_react_tool_absent_when_flag_off(ctx, tmp_path):
    from server.services.whatsapp_bridge_service._service import WhatsAppBridgeService
    svc = WhatsAppBridgeService(ctx)
    svc._get_settings().whatsapp_bridge.outbound_reactions_enabled = False
    spec = await svc._build_inbound_dispatch_spec(
        session_key=DM_KEY,
        chat_id=f"{TRUSTED_PHONE.lstrip('+')}@s.whatsapp.net",
        chat_kind="dm", contact_id=None, is_trusted=True, human_initiated=True)
    names = {t.name for t in spec.tools}
    assert "react_whatsapp_message" not in names
    assert "send_whatsapp_message" in names  # send tool unaffected
