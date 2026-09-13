"""WhatsApp quote-reply context (2026-09-13).

Users quote-reply in WhatsApp; the quoted context used to be dropped on the
floor — the bridge only extracted the quoted-message id from text replies,
and the Python side never read even that. Now the bridge extracts quote
metadata from every message body type and the service persists it on the
stored row, rendered into the prompt as a `[reply to …]` prefix.

Pinned here, at both seams:
- ingestion: `_handle_incoming_message` stores a `quote` block (plus the
  row's own `wa_message_id`) in `messages.metadata`, coexisting with media
  metadata; content stays the raw text (attribution is render-time, like
  `[Sender Name]`).
- rendering: `build_chat_messages` prefixes user rows carrying quote
  metadata, composed INSIDE the dispatch markers (`[NEW] [reply to …] …`)
  and flowing into the lifted trailer lines; rows without quote metadata
  render byte-identical to before.
"""

from __future__ import annotations

import json

import pytest

from server.repositories.history import HistoryRepository
from server.services.prompt_assembler import build_chat_messages
from server.services.session_service import SessionService

from tests.services.test_whatsapp_inbound_characterization import (
    TRUSTED_PHONE,
    _dm_payload,
    _group_payload,
    _make_service,
    _seed_contact,
    _stub_llm,
    _stub_workspace,
    immediate_patience,  # noqa: F401  (fixture re-export)
    stub_memory,  # noqa: F401
)

BOB_PHONE = "+614155550001"
QUOTED_CONTACT_PHONE = "+614000000077"


def _with_quote(payload: dict, *, quoted_id: str = "wamid-quoted-1",
                quoted_sender: str, quoted_text: str) -> dict:
    payload = dict(payload)
    payload["quoted_message_id"] = quoted_id
    payload["quoted_sender_jid"] = f"{quoted_sender.lstrip('+')}@s.whatsapp.net"
    payload["quoted_text"] = quoted_text
    return payload


async def _stored_user_row(db, like: str):
    rows = await db.fetch_all(
        "SELECT * FROM messages WHERE conversation_id LIKE ? AND role='user' "
        "ORDER BY created_at DESC, id", (like,))
    assert rows, "expected a stored user message"
    return rows[0]


# ------------------------------------------------------------- ingestion


@pytest.mark.asyncio
async def test_quote_metadata_persisted_dm(ctx, tmp_path, immediate_patience,
                                           stub_memory, monkeypatch):
    _stub_workspace(monkeypatch)

    async def behaviour(messages, tools):
        return ""
    _stub_llm(monkeypatch, behaviour)

    await _seed_contact(ctx.db, TRUSTED_PHONE)
    svc = _make_service(ctx, tmp_path)
    await svc._handle_incoming_message(_with_quote(
        _dm_payload(TRUSTED_PHONE, "yes that works", msg_id="wamid-q1"),
        quoted_sender=QUOTED_CONTACT_PHONE,
        quoted_text="are we still on for friday?"))

    row = await _stored_user_row(ctx.db, f"agent:main:whatsapp:dm:{TRUSTED_PHONE.lstrip('+')}%")
    # Content stays raw — the quote prefix is render-time only.
    assert row["content"] == "yes that works"
    meta = json.loads(row["metadata"])
    assert meta["wa_message_id"] == "wamid-q1"
    assert meta["quote"] == {
        "wa_message_id": "wamid-quoted-1",
        "sender_jid": f"{QUOTED_CONTACT_PHONE.lstrip('+')}@s.whatsapp.net",
        "sender_name": None,  # unknown sender: no name resolved
        "text": "are we still on for friday?",
    }
    # Ingress event records the quote for greppability.
    event = await ctx.db.fetch_one(
        "SELECT payload_json FROM event_log WHERE external_id = ?", ("wamid-q1",))
    assert json.loads(event["payload_json"])["has_quote"] is True


@pytest.mark.asyncio
async def test_quote_metadata_coexists_with_media(ctx, tmp_path,
                                                  immediate_patience, stub_memory,
                                                  monkeypatch):
    """A reply sent WITH an image is the case the bridge used to drop
    entirely — both media and quote must land on the stored row."""
    _stub_workspace(monkeypatch)

    async def behaviour(messages, tools):
        return ""
    _stub_llm(monkeypatch, behaviour)

    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (media_dir / "wamid-q2.jpg").write_bytes(b"\xff\xd8fakejpg")

    await _seed_contact(ctx.db, TRUSTED_PHONE)
    svc = _make_service(ctx, tmp_path)
    payload = _with_quote(
        _dm_payload(TRUSTED_PHONE, "this one", msg_id="wamid-q2"),
        quoted_sender=QUOTED_CONTACT_PHONE,
        quoted_text="an image")
    payload["media"] = {
        "media_type": "image", "mime_type": "image/jpeg",
        "filename": "wamid-q2.jpg", "size_bytes": 11,
    }
    await svc._handle_incoming_message(payload)

    row = await _stored_user_row(ctx.db, f"agent:main:whatsapp:dm:{TRUSTED_PHONE.lstrip('+')}%")
    meta = json.loads(row["metadata"])
    assert meta["image_path"].endswith("wamid-q2.jpg")
    assert meta["quote"]["text"] == "an image"
    assert meta["wa_message_id"] == "wamid-q2"


@pytest.mark.asyncio
async def test_quote_sender_name_resolved_from_contacts(ctx, tmp_path,
                                                        immediate_patience,
                                                        stub_memory, monkeypatch):
    _stub_workspace(monkeypatch)

    async def behaviour(messages, tools):
        return ""
    _stub_llm(monkeypatch, behaviour)

    await _seed_contact(ctx.db, QUOTED_CONTACT_PHONE, name="Alice Quoted")
    await _seed_contact(ctx.db, TRUSTED_PHONE)
    svc = _make_service(ctx, tmp_path)
    await svc._handle_incoming_message(_with_quote(
        _group_payload(TRUSTED_PHONE, "agreed", msg_id="wamid-q3"),
        quoted_sender=QUOTED_CONTACT_PHONE,
        quoted_text="original proposal"))

    row = await _stored_user_row(ctx.db, "agent:main:whatsapp:group:%")
    assert json.loads(row["metadata"])["quote"]["sender_name"] == "Alice Quoted"


@pytest.mark.asyncio
async def test_quote_of_bobs_own_message_renders_bob_you(ctx, tmp_path,
                                                         immediate_patience,
                                                         stub_memory, monkeypatch):
    _stub_workspace(monkeypatch)

    async def behaviour(messages, tools):
        return ""
    _stub_llm(monkeypatch, behaviour)

    await _seed_contact(ctx.db, TRUSTED_PHONE)
    svc = _make_service(ctx, tmp_path)
    svc._get_settings().whatsapp_bridge.own_phone = BOB_PHONE
    await svc._handle_incoming_message(_with_quote(
        _dm_payload(TRUSTED_PHONE, "correct", msg_id="wamid-q4"),
        quoted_sender=BOB_PHONE,
        quoted_text="my earlier suggestion"))

    row = await _stored_user_row(ctx.db, f"agent:main:whatsapp:dm:{TRUSTED_PHONE.lstrip('+')}%")
    assert json.loads(row["metadata"])["quote"]["sender_name"] == "Bob (you)"


@pytest.mark.asyncio
async def test_plain_message_gets_wa_id_but_no_quote(ctx, tmp_path,
                                                     immediate_patience,
                                                     stub_memory, monkeypatch):
    _stub_workspace(monkeypatch)

    async def behaviour(messages, tools):
        return ""
    _stub_llm(monkeypatch, behaviour)

    await _seed_contact(ctx.db, TRUSTED_PHONE)
    svc = _make_service(ctx, tmp_path)
    await svc._handle_incoming_message(_dm_payload(TRUSTED_PHONE, "plain", msg_id="wamid-p1"))

    row = await _stored_user_row(ctx.db, f"agent:main:whatsapp:dm:{TRUSTED_PHONE.lstrip('+')}%")
    meta = json.loads(row["metadata"])
    assert meta == {"wa_message_id": "wamid-p1"}


@pytest.mark.asyncio
async def test_own_phone_loaded_from_env(monkeypatch):
    """The live service builds settings via from_env — the env var must
    actually reach the field or "Bob (you)" resolution dies silently."""
    from server.config import Settings
    monkeypatch.setenv("BOB_WHATSAPP_BRIDGE_OWN_PHONE", "+614999900001")
    assert Settings.from_env().whatsapp_bridge.own_phone == "+614999900001"
    monkeypatch.setenv("BOB_WHATSAPP_BRIDGE_OWN_PHONE", "")
    assert Settings.from_env().whatsapp_bridge.own_phone == ""


# ------------------------------------------------------------- rendering


async def _build(ctx, db, key: str, **kwargs):
    return await build_chat_messages(
        None, key, db=db, system_content="sys", max_history=50, **kwargs)


def _contents(messages, role: str) -> list[str]:
    return [m["content"] for m in messages
            if m.get("role") == role and isinstance(m.get("content"), str)]


@pytest.mark.asyncio
async def test_render_quote_prefix_group_with_claim_marker(ctx, db):
    """Group row: sender prefix outermost, [NEW] next (dispatch turn), then
    the quote marker — and the trailer lifts the same composed line."""
    key = "agent:main:whatsapp:group:120363000000000001"
    svc = SessionService(ctx)
    contact_id = await _seed_contact(ctx.db, TRUSTED_PHONE, name="Mike T")
    await svc.add_message(
        key, "user", "yes that works", dispatched=0,
        channel="whatsapp", sender_id=contact_id,
        metadata={"quote": {
            "wa_message_id": "wamid-old",
            "sender_jid": "614155550001@s.whatsapp.net",
            "sender_name": "Bob (you)",
            "text": "are we still on for friday?",
        }})
    await db.execute(
        """INSERT INTO conversations (id, kind, created_at, updated_at)
           VALUES (?, 'group', datetime('now'), datetime('now'))
           ON CONFLICT(id) DO NOTHING""", (key,))
    await db.execute(
        """INSERT INTO participants (conversation_id, identifier, display_name,
                                     contact_id, is_trusted, last_active_at)
           VALUES (?, ?, ?, ?, 1, datetime('now'))""",
        (key, TRUSTED_PHONE, "Mike T", contact_id))
    # Mid-turn shape: Bob's prior reply landed AFTER the new message arrived,
    # so replay ends assistant-side and the trailer lifts the claimed row.
    await svc.add_message(key, "assistant", "my earlier reply, already delivered")

    claimed = set(await HistoryRepository(db).pending_user_ids(key))
    messages = await _build(ctx, db, key, claimed_ids=claimed,
                            send_tool_name="send_whatsapp_message")

    expected_row = ('[Mike T] [NEW — awaiting your reply] '
                    '[reply to Bob (you): "are we still on for friday?"] '
                    'yes that works')
    assert any(c == expected_row for c in _contents(messages, "user"))
    # The lifted trailer lines carry the quote prefix too.
    trailer = next(c for c in _contents(messages, "user") if "New message(s)" in c)
    assert '[reply to Bob (you): "are we still on for friday?"]' in trailer


@pytest.mark.asyncio
async def test_render_quote_prefix_dm_unresolved_sender_falls_back_to_digits(ctx, db):
    key = "agent:main:whatsapp:dm:614000000010"
    svc = SessionService(ctx)
    await svc.add_message(
        key, "user", "lol yes", dispatched=0, channel="whatsapp",
        metadata={"quote": {
            "wa_message_id": "wamid-old",
            "sender_jid": "614000000777@s.whatsapp.net",
            "sender_name": None,
            "text": "the original joke",
        }})

    messages = await _build(ctx, db, key)
    assert ('[reply to +614000000777: "the original joke"] lol yes'
            in _contents(messages, "user"))


@pytest.mark.asyncio
async def test_render_quote_without_snippet(ctx, db):
    """Quoted media the bridge couldn't preview: still say who was replied to."""
    key = "agent:main:whatsapp:dm:614000000010"
    svc = SessionService(ctx)
    await svc.add_message(
        key, "user", "about this", dispatched=0, channel="whatsapp",
        metadata={"quote": {
            "wa_message_id": "wamid-old", "sender_jid": None,
            "sender_name": "Alice", "text": None,
        }})

    messages = await _build(ctx, db, key)
    assert "[reply to Alice] about this" in _contents(messages, "user")


@pytest.mark.asyncio
async def test_render_long_snippet_truncated(ctx, db):
    key = "agent:main:whatsapp:dm:614000000010"
    svc = SessionService(ctx)
    await svc.add_message(
        key, "user", "ok", dispatched=0, channel="whatsapp",
        metadata={"quote": {
            "wa_message_id": "wamid-old", "sender_jid": None,
            "sender_name": "Alice", "text": "x" * 250,
        }})

    messages = await _build(ctx, db, key)
    rendered = next(c for c in _contents(messages, "user") if "[reply to" in c)
    assert '"%s…"' % ("x" * 200) in rendered
    assert "xxx" not in rendered.replace("x" * 200, "", 1)


@pytest.mark.asyncio
async def test_render_rows_without_quote_metadata_unchanged(ctx, db):
    key = "agent:main:whatsapp:dm:614000000010"
    svc = SessionService(ctx)
    await svc.add_message(key, "user", "plain old row", dispatched=0, channel="whatsapp")
    await svc.add_message(
        key, "user", "media row", dispatched=0, channel="whatsapp",
        metadata={"image_path": "/nonexistent/wamid-img.jpg"})

    messages = await _build(ctx, db, key)
    contents = _contents(messages, "user")
    assert "plain old row" in contents
    assert any(c.endswith("plain old row") for c in contents), \
        "no quote prefix may appear on quote-less rows"
    # Media stub path (file missing → text fallback) keeps its shape.
    assert any("media row" in c and "[reply to" not in c for c in contents)
