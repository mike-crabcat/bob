"""Characterization tests for the WhatsApp inbound pipeline (Bob3 Phase 0).

These pin CURRENT behaviour of ``_handle_incoming_message`` before the
planned Phase II extraction — they assert what the code does today, not
what it should do. Companions: test_whatsapp_inbound_gate.py (DM
acceptance/drop gate) and test_whatsapp_quota_notify.py (quota notice
rate-limiting).

Notable characterized facts:
- There is NO dedup on whatsapp_message_id inside the service — a replayed
  payload produces a second stored user message. Dedup currently lives in
  the bridge's ack/queue protocol only. (Phase I adds (source, external_id)
  uniqueness at ingress.)
- Groups auto-seed unknown senders as untrusted contacts; DMs drop them.
- The user message is stored with dispatched=0 BEFORE dispatch; assistant
  history is written only from texts actually passed to the send tool
  (delivered-only history); NO_REPLY records nothing.
- A quota failure restores claimed messages to dispatched=0.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bob_server.services.whatsapp_bridge_service import _service as wa_module
from bob_server.services.whatsapp_bridge_service._service import WhatsAppBridgeService

TRUSTED_PHONE = "+614000000010"
GROUP_JID = "120363000000000001@g.us"


async def _seed_contact(db, phone: str, *, trusted: int = 1, allow_inbound: int = 1,
                        name: str | None = None) -> str:
    contact_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO contacts (id, name, phone_number, is_trusted, allow_inbound_dm,
                                 created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
        (contact_id, name if name is not None else f"Contact {phone[-3:]}", phone,
         trusted, allow_inbound),
    )
    return contact_id


def _settings(tmp_path: Path):
    """Real Settings with test paths — build_common_tools walks many fields."""
    from bob_server.config import Settings
    settings = Settings.from_env()
    settings.whatsapp_bridge.media_dir = tmp_path / "media"
    settings.harness.workspace_dir = tmp_path / "workspace"
    settings.dream.enabled = False
    settings.patience.patience_off_settle_seconds = 0.0
    settings.openai.api_key = settings.openai.api_key or "test-key"  # enabled is derived
    return settings


def _make_service(ctx, tmp_path: Path) -> WhatsAppBridgeService:
    svc = object.__new__(WhatsAppBridgeService)
    svc.ctx = ctx
    svc.db = ctx.db
    svc._ws = None  # _send_ack becomes a no-op
    svc._presence_subscribed = set()
    settings = _settings(tmp_path)
    ctx.settings = settings
    svc._get_settings = lambda: settings  # type: ignore[method-assign]
    svc.send_message = AsyncMock(return_value="req-1")  # type: ignore[method-assign]
    svc.send_media = AsyncMock(return_value="req-2")  # type: ignore[method-assign]
    svc.subscribe_presence = AsyncMock()  # type: ignore[method-assign]
    svc._register_send_executors()  # normally done in __init__ (Phase IV effects)
    return svc


@pytest.fixture
def immediate_patience(monkeypatch):
    """Bypass the attention coordinator: run the dispatch closure immediately."""
    async def _submit(self, session_key, dispatch_fn, **kwargs):
        await dispatch_fn()
    monkeypatch.setattr(
        "bob_server.services.attention.coordinator.AttentionCoordinator.submit", _submit)
    return _submit


@pytest.fixture
def stub_memory(monkeypatch):
    """Person-memory side effects are filesystem writes — stub them out."""
    stub = SimpleNamespace(
        ensure_person_entry=AsyncMock(),
        find_person_entry=AsyncMock(return_value=None),
        sync_person_display_name_for_contact=AsyncMock(),
    )

    class _StubMemoryService:
        def __init__(self, ctx):
            pass
        ensure_person_entry = staticmethod(stub.ensure_person_entry)
        find_person_entry = staticmethod(stub.find_person_entry)
        sync_person_display_name_for_contact = staticmethod(
            stub.sync_person_display_name_for_contact)

    import bob_server.services.memory as memory_pkg
    monkeypatch.setattr(memory_pkg, "MemoryService", _StubMemoryService)
    return stub


def _stub_llm(monkeypatch, behaviour):
    """Replace chat_with_tools; `behaviour(messages, tools)` returns text."""
    from bob_server.services.llm_dispatch import LLMDispatchService

    async def _chat_with_tools(self, messages, tools, **kwargs):
        return await behaviour(messages, tools)

    monkeypatch.setattr(LLMDispatchService, "chat_with_tools", _chat_with_tools)


def _stub_workspace(monkeypatch):
    async def _load(workspace_dir, db=None):
        return "workspace prompt"
    monkeypatch.setattr(
        "bob_server.services.prompt_assembler.load_workspace_prompt", _load)


async def _get_tool(tools, name):
    return next(t for t in tools if t.name == name)


def _dm_payload(phone: str, text: str, msg_id: str = "wamid-1") -> dict:
    return {
        "chat_id": f"{phone.lstrip('+')}@s.whatsapp.net",
        "chat_kind": "dm",
        "sender_jid": f"{phone.lstrip('+')}@s.whatsapp.net",
        "sender_name": "Tester",
        "text": text,
        "whatsapp_message_id": msg_id,
        "mentioned_jids": [],
        "media": None,
    }


def _group_payload(sender_phone: str, text: str, msg_id: str = "wamid-g1") -> dict:
    return {
        "chat_id": GROUP_JID,
        "chat_kind": "group",
        "sender_jid": f"{sender_phone.lstrip('+')}@s.whatsapp.net",
        "sender_name": "Group Sender",
        "text": text,
        "whatsapp_message_id": msg_id,
        "mentioned_jids": [],
        "media": None,
    }


async def _user_messages(db, like: str) -> list:
    return await db.fetch_all(
        "SELECT * FROM session_messages WHERE session_key LIKE ? AND role='user' "
        "ORDER BY id", (like,))


async def _assistant_messages(db, like: str) -> list:
    return await db.fetch_all(
        "SELECT * FROM session_messages WHERE session_key LIKE ? AND role='assistant' "
        "ORDER BY id", (like,))


# ------------------------------------------------------------------ seeding


async def test_group_message_from_unknown_sender_auto_seeds_untrusted_contact(
        ctx, tmp_path, immediate_patience, stub_memory, monkeypatch):
    _stub_workspace(monkeypatch)

    async def behaviour(messages, tools):
        return ""
    _stub_llm(monkeypatch, behaviour)

    svc = _make_service(ctx, tmp_path)
    await svc._handle_incoming_message(_group_payload("+614000000099", "hello group"))

    row = await ctx.db.fetch_one(
        "SELECT * FROM contacts WHERE phone_number = ?", ("+614000000099",))
    assert row is not None, "group senders are auto-seeded"
    assert row["is_trusted"] == 0
    assert row["name"] == "Group Sender"
    stub_memory.ensure_person_entry.assert_awaited_once()


async def test_dm_name_backfill_when_contact_name_is_phone_number(
        ctx, tmp_path, immediate_patience, stub_memory, monkeypatch):
    _stub_workspace(monkeypatch)

    async def behaviour(messages, tools):
        return ""
    _stub_llm(monkeypatch, behaviour)

    contact_id = await _seed_contact(
        ctx.db, TRUSTED_PHONE, trusted=1, name=TRUSTED_PHONE)
    svc = _make_service(ctx, tmp_path)
    await svc._handle_incoming_message(_dm_payload(TRUSTED_PHONE, "hi"))

    row = await ctx.db.fetch_one(
        "SELECT name FROM contacts WHERE id = ?", (contact_id,))
    assert row["name"] == "Tester"
    stub_memory.sync_person_display_name_for_contact.assert_awaited_once()


# ---------------------------------------------------------------- ordering


async def test_user_message_stored_undispatched_before_llm_runs(
        ctx, tmp_path, immediate_patience, stub_memory, monkeypatch):
    """Store-then-dispatch: the user row exists (dispatched=0 at insert) and
    is claimed (dispatched=1) by the dispatch that runs it."""
    _stub_workspace(monkeypatch)
    seen_at_dispatch: dict = {}

    async def behaviour(messages, tools):
        rows = await ctx.db.fetch_all(
            "SELECT dispatched FROM session_messages WHERE role='user'")
        seen_at_dispatch["rows"] = rows
        return ""
    _stub_llm(monkeypatch, behaviour)

    await _seed_contact(ctx.db, TRUSTED_PHONE)
    svc = _make_service(ctx, tmp_path)
    await svc._handle_incoming_message(_dm_payload(TRUSTED_PHONE, "are you there?"))

    assert len(seen_at_dispatch["rows"]) == 1, "user row persisted before LLM call"
    assert seen_at_dispatch["rows"][0]["dispatched"] == 1, "claimed by mark_dispatched"


async def test_no_dedup_on_whatsapp_message_id(
        ctx, tmp_path, immediate_patience, stub_memory, monkeypatch):
    """CHARACTERIZATION: replaying the exact same payload stores a second
    user message — the service has no (source, external_id) idempotency.
    Phase I moves dedup into the durable inbox; this test documents the
    current hole."""
    _stub_workspace(monkeypatch)

    async def behaviour(messages, tools):
        return ""
    _stub_llm(monkeypatch, behaviour)

    await _seed_contact(ctx.db, TRUSTED_PHONE)
    svc = _make_service(ctx, tmp_path)
    payload = _dm_payload(TRUSTED_PHONE, "same message", msg_id="wamid-dup")
    await svc._handle_incoming_message(payload)
    await svc._handle_incoming_message(dict(payload))

    rows = await _user_messages(ctx.db, "agent:main:whatsapp:dm:%")
    assert len(rows) == 2


# ------------------------------------------------------- delivery semantics


async def test_no_reply_sends_nothing_and_records_no_assistant_history(
        ctx, tmp_path, immediate_patience, stub_memory, monkeypatch):
    _stub_workspace(monkeypatch)

    async def behaviour(messages, tools):
        tool = await _get_tool(tools, "send_whatsapp_message")
        out = await tool.handler(text="NO_REPLY")
        assert out == "No reply sent."
        return "NO_REPLY"
    _stub_llm(monkeypatch, behaviour)

    await _seed_contact(ctx.db, TRUSTED_PHONE)
    svc = _make_service(ctx, tmp_path)
    await svc._handle_incoming_message(_dm_payload(TRUSTED_PHONE, "fyi only"))

    svc.send_message.assert_not_awaited()
    assert await _assistant_messages(ctx.db, "agent:main:whatsapp:dm:%") == []


async def test_assistant_history_is_delivered_only(
        ctx, tmp_path, immediate_patience, stub_memory, monkeypatch):
    """History records exactly the texts passed to the send tool, not the
    model's raw text output."""
    _stub_workspace(monkeypatch)

    async def behaviour(messages, tools):
        tool = await _get_tool(tools, "send_whatsapp_message")
        await tool.handler(text="delivered reply")
        return "raw model text that was never sent"
    _stub_llm(monkeypatch, behaviour)

    await _seed_contact(ctx.db, TRUSTED_PHONE)
    svc = _make_service(ctx, tmp_path)
    await svc._handle_incoming_message(_dm_payload(TRUSTED_PHONE, "question"))

    svc.send_message.assert_awaited_once()
    rows = await _assistant_messages(ctx.db, "agent:main:whatsapp:dm:%")
    assert [r["content"] for r in rows] == ["delivered reply"]


async def test_text_output_without_send_tool_records_nothing(
        ctx, tmp_path, immediate_patience, stub_memory, monkeypatch):
    """If the model replies in plain text without calling the send tool
    (tap disabled), nothing is delivered and nothing enters history."""
    _stub_workspace(monkeypatch)
    monkeypatch.setattr("bob_server.services.tap.tap_enabled", lambda: False)

    async def behaviour(messages, tools):
        return "I forgot to call the tool"
    _stub_llm(monkeypatch, behaviour)

    await _seed_contact(ctx.db, TRUSTED_PHONE)
    svc = _make_service(ctx, tmp_path)
    await svc._handle_incoming_message(_dm_payload(TRUSTED_PHONE, "hello"))

    svc.send_message.assert_not_awaited()
    assert await _assistant_messages(ctx.db, "agent:main:whatsapp:dm:%") == []


# ----------------------------------------------------------- quota restore


async def test_quota_error_restores_claimed_messages_for_retry(
        ctx, tmp_path, immediate_patience, stub_memory, monkeypatch):
    _stub_workspace(monkeypatch)

    async def behaviour(messages, tools):
        raise RuntimeError("insufficient_quota: billing hard limit reached")
    _stub_llm(monkeypatch, behaviour)
    notify = AsyncMock()
    monkeypatch.setattr(wa_module, "_notify_quota_exhausted", notify)

    await _seed_contact(ctx.db, TRUSTED_PHONE)
    svc = _make_service(ctx, tmp_path)
    await svc._handle_incoming_message(_dm_payload(TRUSTED_PHONE, "hi"))

    rows = await _user_messages(ctx.db, "agent:main:whatsapp:dm:%")
    assert len(rows) == 1
    assert rows[0]["dispatched"] == 0, "restored for retry after quota failure"
    notify.assert_awaited_once()


async def test_non_quota_llm_error_leaves_messages_claimed(
        ctx, tmp_path, immediate_patience, stub_memory, monkeypatch):
    """CHARACTERIZATION: a non-quota LLM failure propagates and the claimed
    message stays dispatched=1 — it is silently consumed (invariant 6 gap;
    fixed by turns/attempts in Phase III)."""
    _stub_workspace(monkeypatch)

    async def behaviour(messages, tools):
        raise RuntimeError("boom: transient upstream failure")
    _stub_llm(monkeypatch, behaviour)

    await _seed_contact(ctx.db, TRUSTED_PHONE)
    svc = _make_service(ctx, tmp_path)
    with pytest.raises(RuntimeError, match="boom"):
        await svc._handle_incoming_message(_dm_payload(TRUSTED_PHONE, "hi"))

    rows = await _user_messages(ctx.db, "agent:main:whatsapp:dm:%")
    assert rows[0]["dispatched"] == 1


# ------------------------------------------------------ burst / concurrency


async def test_burst_messages_claimed_by_single_dispatch(
        ctx, tmp_path, stub_memory, monkeypatch):
    """Two arrivals before the first dispatch runs → the first dispatch
    claims both, the second dispatch finds nothing and skips the LLM."""
    _stub_workspace(monkeypatch)

    dispatch_fns = []

    async def _capture(self, session_key, dispatch_fn, **kwargs):
        dispatch_fns.append(dispatch_fn)
    monkeypatch.setattr(
        "bob_server.services.attention.coordinator.AttentionCoordinator.submit", _capture)

    llm_calls = []

    async def behaviour(messages, tools):
        llm_calls.append(messages)
        return ""
    _stub_llm(monkeypatch, behaviour)

    await _seed_contact(ctx.db, TRUSTED_PHONE)
    svc = _make_service(ctx, tmp_path)
    await svc._handle_incoming_message(_dm_payload(TRUSTED_PHONE, "first", msg_id="w1"))
    await svc._handle_incoming_message(_dm_payload(TRUSTED_PHONE, "second", msg_id="w2"))

    assert len(dispatch_fns) == 2
    for fn in dispatch_fns:
        await fn()

    rows = await _user_messages(ctx.db, "agent:main:whatsapp:dm:%")
    assert [r["dispatched"] for r in rows] == [1, 1]
    assert len(llm_calls) == 1, "second dispatch found no unclaimed messages"


# ------------------------------------------------- failure injection: send

async def test_send_failure_after_claim_records_recoverable_effect(
        ctx, tmp_path, immediate_patience, stub_memory, monkeypatch):
    """FAILURE INJECTION at the send boundary: transport dies during
    send_whatsapp_message. Bob3 Phase IV: the send is recorded as an effect
    before delivery — the failure no longer propagates, the effect stays
    pending for the pump to retry, and no assistant history is written
    (delivered-only). Closes the pre-Bob3 lost-turn gap."""
    _stub_workspace(monkeypatch)

    async def behaviour(messages, tools):
        tool = await _get_tool(tools, "send_whatsapp_message")
        result = await tool.handler(text="about to fail")
        assert result.startswith("Error sending message")
        return "unreachable"
    _stub_llm(monkeypatch, behaviour)

    await _seed_contact(ctx.db, TRUSTED_PHONE)
    svc = _make_service(ctx, tmp_path)
    svc.send_message = AsyncMock(side_effect=ConnectionError("bridge died mid-send"))

    await svc._handle_incoming_message(_dm_payload(TRUSTED_PHONE, "hello"))

    rows = await _user_messages(ctx.db, "agent:main:whatsapp:dm:%")
    assert rows[0]["dispatched"] == 1
    assert await _assistant_messages(ctx.db, "agent:main:whatsapp:dm:%") == []
    effect = await ctx.db.fetch_one(
        "SELECT * FROM effects WHERE kind = 'whatsapp_send'")
    assert effect is not None
    assert effect["status"] == "pending", "failed send awaits pump retry"


async def test_crash_between_store_and_dispatch_leaves_message_recoverable(
        ctx, tmp_path, stub_memory, monkeypatch):
    """FAILURE INJECTION between store and dispatch: the process dies after
    the user message is persisted but before the dispatch closure runs.
    The message survives as dispatched=0 — a later dispatch on the same
    session picks it up (the property burst-claiming relies on)."""
    _stub_workspace(monkeypatch)

    async def _drop(self, session_key, dispatch_fn, **kwargs):
        return  # simulated crash: dispatch never runs
    monkeypatch.setattr(
        "bob_server.services.attention.coordinator.AttentionCoordinator.submit", _drop)

    llm_calls = []

    async def behaviour(messages, tools):
        llm_calls.append(1)
        return ""
    _stub_llm(monkeypatch, behaviour)

    await _seed_contact(ctx.db, TRUSTED_PHONE)
    svc = _make_service(ctx, tmp_path)
    await svc._handle_incoming_message(_dm_payload(TRUSTED_PHONE, "orphaned", msg_id="w-crash"))

    rows = await _user_messages(ctx.db, "agent:main:whatsapp:dm:%")
    assert rows[0]["dispatched"] == 0, "message survives the crash unclaimed"

    # a later arrival's dispatch claims both messages
    async def _run(self, sk, fn, **kw):
        await fn()
    monkeypatch.setattr(
        "bob_server.services.attention.coordinator.AttentionCoordinator.submit", _run)
    await svc._handle_incoming_message(_dm_payload(TRUSTED_PHONE, "follow-up", msg_id="w-next"))

    rows = await _user_messages(ctx.db, "agent:main:whatsapp:dm:%")
    assert [r["dispatched"] for r in rows] == [1, 1]
    assert len(llm_calls) == 1


# ------------------------------------------------------------- empty/media


async def test_empty_message_without_media_is_ignored(
        ctx, tmp_path, immediate_patience, stub_memory, monkeypatch):
    _stub_workspace(monkeypatch)
    llm = AsyncMock(return_value="")
    _stub_llm(monkeypatch, lambda m, t: llm(m, t))

    await _seed_contact(ctx.db, TRUSTED_PHONE)
    svc = _make_service(ctx, tmp_path)
    payload = _dm_payload(TRUSTED_PHONE, "")
    await svc._handle_incoming_message(payload)

    assert await _user_messages(ctx.db, "agent:main:whatsapp:dm:%") == []
    llm.assert_not_awaited()


async def test_media_filename_escaping_media_dir_is_ignored(
        ctx, tmp_path, immediate_patience, stub_memory, monkeypatch):
    """Path traversal in media filename resolves outside media_dir and is
    treated as no-media; with empty text the message is dropped entirely."""
    _stub_workspace(monkeypatch)
    llm = AsyncMock(return_value="")
    _stub_llm(monkeypatch, lambda m, t: llm(m, t))

    (tmp_path / "media").mkdir(parents=True)
    secret = tmp_path / "secret.jpg"
    secret.write_bytes(b"x")

    await _seed_contact(ctx.db, TRUSTED_PHONE)
    svc = _make_service(ctx, tmp_path)
    payload = _dm_payload(TRUSTED_PHONE, "")
    payload["media"] = {"media_type": "image", "filename": "../secret.jpg",
                        "mime_type": "image/jpeg"}
    await svc._handle_incoming_message(payload)

    assert await _user_messages(ctx.db, "agent:main:whatsapp:dm:%") == []
    llm.assert_not_awaited()
