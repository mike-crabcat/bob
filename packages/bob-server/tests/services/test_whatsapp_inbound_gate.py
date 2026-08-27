"""Tests for the inbound WhatsApp DM gate against allow_inbound_dm.

The gate lives early in ``_handle_incoming_message``: a DM from a number whose
contact row has ``allow_inbound_dm = 0`` (or from an unknown number) must be
dropped before any session side effects. Group messages are exempt.

The pass-through cases use slash-command text ("/who") so the method returns
at the slash interception — just past the gate — without entering the
dispatch machinery.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bob_server.services.whatsapp_bridge_service._service import WhatsAppBridgeService

HUMAN_DM = "+614000000001"      # trusted, inbound allowed
OUTBOUND_ONLY = "+614000000002"  # untrusted, inbound disabled
RESTRICTED_HUMAN = "+614000000003"  # trusted, but inbound disabled


async def _seed_contact(db, phone: str, *, trusted: int, allow_inbound: int) -> str:
    contact_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO contacts (id, name, phone_number, is_trusted, allow_inbound_dm,
                                 created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
        (contact_id, f"Contact {phone[-3:]}", phone, trusted, allow_inbound),
    )
    return contact_id


def _make_service(db) -> tuple[WhatsAppBridgeService, AsyncMock]:
    svc = object.__new__(WhatsAppBridgeService)
    svc.db = db
    svc.ctx = SimpleNamespace(db=db)  # channel policy resolves sender via ctx
    svc._ws = None  # _send_ack becomes a no-op
    svc._get_settings = lambda: SimpleNamespace(  # type: ignore[method-assign]
        openai=SimpleNamespace(enabled=True),
        whatsapp_bridge=SimpleNamespace(media_dir=None),
    )
    slash = AsyncMock()
    svc._handle_slash_command = slash  # type: ignore[method-assign]
    return svc, slash


def _dm_payload(phone: str, text: str = "/who") -> dict:
    return {
        "chat_id": f"{phone.lstrip('+')}@s.whatsapp.net",
        "chat_kind": "dm",
        "sender_jid": f"{phone.lstrip('+')}@s.whatsapp.net",
        "sender_name": "Tester",
        "text": text,
        "whatsapp_message_id": "wamid-test",
        "mentioned_jids": [],
        "media": None,
    }


async def _participant_rows(db, phone: str) -> list:
    return await db.fetch_all(
        "SELECT * FROM participants WHERE identifier = ?", (phone,),
    )


async def test_dm_from_allowed_contact_passes_gate(db):
    await _seed_contact(db, HUMAN_DM, trusted=1, allow_inbound=1)
    svc, slash = _make_service(db)
    await svc._handle_incoming_message(_dm_payload(HUMAN_DM))
    slash.assert_awaited_once()


async def test_dm_from_outbound_only_contact_is_dropped(db):
    await _seed_contact(db, OUTBOUND_ONLY, trusted=0, allow_inbound=0)
    svc, slash = _make_service(db)
    await svc._handle_incoming_message(_dm_payload(OUTBOUND_ONLY))
    slash.assert_not_awaited()
    assert await _participant_rows(db, OUTBOUND_ONLY) == []


async def test_dm_from_restricted_trusted_contact_is_dropped(db):
    """Trusted but inbound-disabled: trust alone must not open the DM gate."""
    await _seed_contact(db, RESTRICTED_HUMAN, trusted=1, allow_inbound=0)
    svc, slash = _make_service(db)
    await svc._handle_incoming_message(_dm_payload(RESTRICTED_HUMAN))
    slash.assert_not_awaited()
    assert await _participant_rows(db, RESTRICTED_HUMAN) == []


async def test_dm_from_unknown_number_is_dropped(db):
    svc, slash = _make_service(db)
    await svc._handle_incoming_message(_dm_payload("+619999999999"))
    slash.assert_not_awaited()
    assert await _participant_rows(db, "+619999999999") == []


async def test_group_message_from_outbound_only_contact_passes_gate(db):
    """The gate is DM-only; a flag-0 contact in a group chat is unaffected."""
    await _seed_contact(db, OUTBOUND_ONLY, trusted=1, allow_inbound=0)
    svc, slash = _make_service(db)
    payload = _dm_payload(OUTBOUND_ONLY)
    payload["chat_kind"] = "group"
    payload["chat_id"] = "120363422982048691@g.us"
    await svc._handle_incoming_message(payload)
    slash.assert_awaited_once()


async def test_outreach_tool_refuses_outbound_only_contact(db):
    """send_whatsapp_to_contact fails for allow_inbound_dm=0 contacts.

    The outbound mirror of the inbound gate above: outreach to an
    outbound-only contact invites a reply that _handle_incoming_message
    will drop, stranding the outreach goal. The tool must fail loudly
    instead of sending.
    """
    import json

    from bob_server.services.whatsapp_outreach_tools import (
        make_whatsapp_outreach_tools,
    )

    contact_id = await _seed_contact(db, OUTBOUND_ONLY, trusted=0, allow_inbound=0)
    sent: list[tuple[str, str]] = []

    class _FakeBridge:
        connected = True

        async def send_message(self, jid, message):
            sent.append((jid, message))
            return "req-1"

    ctx = SimpleNamespace(db=db)
    tools = {t.name: t for t in
             make_whatsapp_outreach_tools(ctx, _FakeBridge(), "work")}
    out = json.loads(await tools["send_whatsapp_to_contact"].handler(
        contact_id=contact_id, message="hey", objective="say hi"))
    assert not out["ok"]
    assert "outbound-only" in out["error"]
    assert sent == []  # nothing left the bridge
