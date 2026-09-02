"""Characterization tests for the current inbound email pipeline.

No impractical behaviours were skipped. Quota/retry is characterized by the
current absence of email-specific quota handling: an LLM quota-looking failure
marks the inbound user message dispatched and records no assistant response.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.services.email_polling_service import (
    KNOWN_UNTRUSTED_AGENDA,
    UNTRUSTED_EXTERNAL_AGENDA,
    EmailPollingService,
)


async def _count(db, table_where: str, params: tuple = ()) -> int:
    row = await db.fetch_one(f"SELECT COUNT(*) AS n FROM {table_where}", params)
    return row["n"]


async def _seed_inbox(db) -> dict:
    inbox_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO email_inboxes
           (id, agentmail_inbox_id, display_name, email_address, created_at, updated_at)
           VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))""",
        (inbox_id, "agentmail-inbox", "Bob", "bob@example.com"),
    )
    return await db.fetch_one("SELECT * FROM email_inboxes WHERE id = ?", (inbox_id,))


async def _seed_contact(db, email: str, *, trusted: int, name: str = "Sender") -> str:
    contact_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO contacts (id, name, email, is_trusted, created_at, updated_at)
           VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))""",
        (contact_id, name, email, trusted),
    )
    return contact_id


def _message(
    *,
    message_id: str = "msg-1",
    thread_id: str = "thread-1",
    sender: str | dict = '"Alice Example" <alice@example.com>',
    body: str = "Hello from email",
) -> dict:
    return {
        "message_id": message_id,
        "thread_id": thread_id,
        "from": sender,
        "to": [{"email": "bob@example.com", "name": "Bob"}],
        "cc": [],
        "subject": "Question",
        "extracted_text": body,
        "labels": ["unread"],
        "timestamp": "2026-08-22T10:00:00+00:00",
    }


def _make_service(ctx) -> tuple[EmailPollingService, SimpleNamespace]:
    ctx.settings.openai.api_key = "test-key"
    ctx.settings.harness.workspace_dir = Path("/home/bob/bob")
    ctx.settings.harness.skill_dev_enabled = False
    ctx.settings.dream.enabled = False
    ctx.settings.phone.enabled = False
    ctx.settings.homeassistant.enabled = False
    client = SimpleNamespace(update_message=AsyncMock(), get_attachment=AsyncMock())
    return EmailPollingService(ctx, agentmail_client=client), client


def _capture_create_task(monkeypatch) -> list:
    created = []

    def fake_create_task(coro):
        created.append(coro)
        return SimpleNamespace()

    monkeypatch.setattr(asyncio, "create_task", fake_create_task)
    return created


async def _create_thread_for_sender(ctx, db, *, trusted: int) -> dict:
    inbox = await _seed_inbox(db)
    contact_id = await _seed_contact(
        db,
        "sender@example.com",
        trusted=trusted,
        name="Trusted Sender" if trusted else "Known Sender",
    )
    svc, _ = _make_service(ctx)
    thread, _ = await svc._resolve_or_create_thread(
        inbox,
        _message(sender='"Sender" <sender@example.com>'),
        "thread-tools",
        SimpleNamespace(isoformat=lambda: "2026-08-22T10:00:00+00:00"),
    )
    assert thread["contact_id"] == contact_id
    return thread


async def _run_dispatch_and_capture(ctx, db, monkeypatch, *, trusted: int, result: str = "") -> dict:
    thread = await _create_thread_for_sender(ctx, db, trusted=trusted)
    inbox = await db.fetch_one("SELECT * FROM email_inboxes WHERE id = ?", (thread["inbox_id"],))
    svc, _ = _make_service(ctx)
    tasks = _capture_create_task(monkeypatch)
    captured: dict = {}

    async def fake_chat_with_tools(self, messages, tools, **kwargs):
        captured["messages"] = messages
        captured["tools"] = tools
        captured["kwargs"] = kwargs
        return result

    monkeypatch.setattr(
        "server.services.llm_dispatch.LLMDispatchService.chat_with_tools",
        fake_chat_with_tools,
    )

    await svc._dispatch_to_llm(
        thread,
        _message(sender='"Sender" <sender@example.com>'),
        inbox,
        is_new_thread=False,
    )
    assert len(tasks) == 1
    await tasks[0]
    return {"captured": captured, "thread": thread}


async def test_unknown_sender_is_auto_seeded_untrusted_and_processing_continues(ctx, db, monkeypatch):
    inbox = await _seed_inbox(db)
    svc, client = _make_service(ctx)
    tasks = _capture_create_task(monkeypatch)

    processed = await svc.process_incoming_message(
        inbox,
        _message(sender='"Alice Example" <alice@example.com>'),
    )

    assert processed is True
    client.update_message.assert_awaited_once_with(
        "agentmail-inbox", "msg-1", remove_labels=["unread"]
    )
    assert len(tasks) == 1
    tasks[0].close()

    contact = await db.fetch_one("SELECT * FROM contacts WHERE email = ?", ("alice@example.com",))
    assert contact["name"] == "Alice Example"
    assert contact["phone_number"] is None
    assert contact["is_trusted"] == 0
    assert contact["allow_inbound_dm"] == 1

    thread = await db.fetch_one("SELECT * FROM email_threads WHERE agentmail_thread_id = ?", ("thread-1",))
    assert thread["contact_id"] == contact["id"]
    assert thread["agenda"] == KNOWN_UNTRUSTED_AGENDA
    assert thread["message_count"] == 1

    stored = await db.fetch_one("SELECT * FROM email_messages WHERE agentmail_message_id = ?", ("msg-1",))
    assert stored["sender_email"] == "alice@example.com"
    assert stored["sender_name"] == "Alice Example"


async def test_missing_sender_stays_external_untrusted(ctx, db, monkeypatch):
    inbox = await _seed_inbox(db)
    svc, _ = _make_service(ctx)
    tasks = _capture_create_task(monkeypatch)

    assert await svc.process_incoming_message(inbox, _message(sender=None)) is True
    tasks[0].close()

    assert await db.fetch_one("SELECT * FROM contacts") is None
    thread = await db.fetch_one("SELECT * FROM email_threads WHERE agentmail_thread_id = ?", ("thread-1",))
    assert thread["contact_id"] is None
    assert thread["agenda"] == UNTRUSTED_EXTERNAL_AGENDA


async def test_duplicate_message_id_does_not_create_duplicate_rows_or_dispatch_after_history_exists(ctx, db, monkeypatch):
    inbox = await _seed_inbox(db)
    svc, _ = _make_service(ctx)
    calls = []

    async def fake_dispatch(thread, message, inbox, **kwargs):
        calls.append((thread, message, kwargs))
        await db.execute(
            """INSERT INTO messages (id, conversation_id, role, content, channel, dispatched)
               VALUES (?, ?, 'user', 'already dispatched', 'email', 1)""",
            (str(uuid.uuid4()), thread["session_key"]),
        )

    svc._dispatch_email_safe = fake_dispatch  # type: ignore[method-assign]
    tasks = _capture_create_task(monkeypatch)

    assert await svc.process_incoming_message(inbox, _message()) is True
    assert len(tasks) == 1
    await tasks[0]

    assert await svc.process_incoming_message(inbox, _message()) is False
    assert len(tasks) == 1
    assert len(calls) == 1
    assert await _count(db, "email_messages") == 1
    assert await _count(db, "messages") == 1
    thread = await db.fetch_one("SELECT * FROM email_threads WHERE agentmail_thread_id = ?", ("thread-1",))
    assert thread["message_count"] == 1


async def test_duplicate_backfilled_message_is_redispatched_when_no_session_history(ctx, db, monkeypatch):
    inbox = await _seed_inbox(db)
    svc, _ = _make_service(ctx)
    tasks = _capture_create_task(monkeypatch)

    assert await svc.process_incoming_message(inbox, _message(), backfill=True) is True
    assert tasks == []

    assert await svc.process_incoming_message(inbox, _message()) is False
    assert len(tasks) == 1
    tasks[0].close()
    assert await _count(db, "email_messages") == 1


async def test_trusted_email_dispatch_gets_trust_escalated_tools(ctx, db, monkeypatch):
    result = await _run_dispatch_and_capture(ctx, db, monkeypatch, trusted=1, result="")
    tool_names = {tool.name for tool in result["captured"]["tools"]}

    assert "email_reply" in tool_names
    assert "email_skip" in tool_names
    assert "create_contact" in tool_names
    assert result["captured"]["kwargs"]["contact_id"] == result["thread"]["contact_id"]


async def test_untrusted_email_dispatch_omits_trust_escalated_contact_creation(ctx, db, monkeypatch):
    result = await _run_dispatch_and_capture(ctx, db, monkeypatch, trusted=0, result="")
    tool_names = {tool.name for tool in result["captured"]["tools"]}

    assert "email_reply" in tool_names
    assert "email_skip" in tool_names
    assert "create_contact" not in tool_names
    assert result["captured"]["kwargs"]["contact_id"] == result["thread"]["contact_id"]


async def test_no_reply_text_is_not_sent_but_is_recorded_as_assistant_history(ctx, db, monkeypatch):
    result = await _run_dispatch_and_capture(ctx, db, monkeypatch, trusted=0, result="NO_REPLY")
    thread = result["thread"]

    rows = await db.fetch_all(
        "SELECT role, content, dispatched FROM messages WHERE conversation_id = ? ORDER BY role DESC",
        (thread["session_key"],),
    )
    assert [(r["role"], r["content"], r["dispatched"]) for r in rows] == [
        ("user", "[Email from: Sender <sender@example.com>]\n[Subject: Question]\n\nHello from email", 1),
        ("assistant", "NO_REPLY", 1),
    ]


async def test_assistant_history_is_written_after_successful_email_reply_send(ctx, db, monkeypatch):
    thread = await _create_thread_for_sender(ctx, db, trusted=1)
    inbox = await db.fetch_one("SELECT * FROM email_inboxes WHERE id = ?", (thread["inbox_id"],))
    svc, _ = _make_service(ctx)
    tasks = _capture_create_task(monkeypatch)
    assistant_counts_seen_during_send = []

    async def fake_send_reply(self, **kwargs):
        assistant_counts_seen_during_send.append(
            await _count(db, "messages WHERE role = 'assistant'")
        )

    async def fake_chat_with_tools(self, messages, tools, **kwargs):
        reply_tool = next(tool for tool in tools if tool.name == "email_reply")
        tool_result = await reply_tool.handler("Delivered body")
        assert '"ok": true' in tool_result
        return "Model text"

    monkeypatch.setattr(
        "server.services.email_delivery_service.EmailDeliveryService.send_reply",
        fake_send_reply,
    )
    monkeypatch.setattr(
        "server.services.llm_dispatch.LLMDispatchService.chat_with_tools",
        fake_chat_with_tools,
    )

    await svc._dispatch_to_llm(thread, _message(sender='"Sender" <sender@example.com>'), inbox)
    await tasks[0]

    assert assistant_counts_seen_during_send == [0]
    assistant = await db.fetch_one(
        "SELECT content FROM messages WHERE conversation_id = ? AND role = 'assistant'",
        (thread["session_key"],),
    )
    assert assistant["content"] == "Model text\n\nDelivered body"


async def test_failed_email_reply_is_not_marked_sent_but_error_text_is_recorded(ctx, db, monkeypatch):
    thread = await _create_thread_for_sender(ctx, db, trusted=1)
    inbox = await db.fetch_one("SELECT * FROM email_inboxes WHERE id = ?", (thread["inbox_id"],))
    svc, _ = _make_service(ctx)
    tasks = _capture_create_task(monkeypatch)

    async def fake_send_reply(self, **kwargs):
        raise RuntimeError("smtp boom")

    async def fake_chat_with_tools(self, messages, tools, **kwargs):
        reply_tool = next(tool for tool in tools if tool.name == "email_reply")
        return await reply_tool.handler("Body that fails")

    monkeypatch.setattr(
        "server.services.email_delivery_service.EmailDeliveryService.send_reply",
        fake_send_reply,
    )
    monkeypatch.setattr(
        "server.services.llm_dispatch.LLMDispatchService.chat_with_tools",
        fake_chat_with_tools,
    )

    await svc._dispatch_to_llm(thread, _message(sender='"Sender" <sender@example.com>'), inbox)
    await tasks[0]

    assistant = await db.fetch_one(
        "SELECT content FROM messages WHERE conversation_id = ? AND role = 'assistant'",
        (thread["session_key"],),
    )
    assert assistant["content"] == "Error sending reply: smtp boom"


async def test_quota_like_llm_failure_has_no_email_retry_restoration(ctx, db, monkeypatch):
    thread = await _create_thread_for_sender(ctx, db, trusted=1)
    inbox = await db.fetch_one("SELECT * FROM email_inboxes WHERE id = ?", (thread["inbox_id"],))
    svc, _ = _make_service(ctx)
    tasks = _capture_create_task(monkeypatch)

    async def fake_chat_with_tools(self, messages, tools, **kwargs):
        raise RuntimeError("insufficient_quota")

    monkeypatch.setattr(
        "server.services.llm_dispatch.LLMDispatchService.chat_with_tools",
        fake_chat_with_tools,
    )

    await svc._dispatch_to_llm(thread, _message(sender='"Sender" <sender@example.com>'), inbox)
    with pytest.raises(RuntimeError, match="insufficient_quota"):
        await tasks[0]

    rows = await db.fetch_all(
        "SELECT role, content, dispatched FROM messages WHERE conversation_id = ? ORDER BY created_at",
        (thread["session_key"],),
    )
    assert [(r["role"], r["dispatched"]) for r in rows] == [("user", 1)]
