"""Steerable email threads (2026-09-23): wake_conversation dispatches email
sessions with the thread's REAL toolset (email_reply + common tools), and the
dashboard wake endpoint (the `bob steer` surface) accepts email-thread
bindings. Operator-path only in v1 — resolve_target still matches DMs and
groups, so contacts can't request email steers yet.
"""

from __future__ import annotations

import pytest

from server.services.session_service import SessionService

THREAD_KEY = "agent:main:email:thread:11111111-2222-3333-4444-555555555555"


@pytest.fixture
def stub_llm(monkeypatch):
    from server.services.llm_dispatch import LLMDispatchService

    seen: dict = {"reply": "No reply needed — internal wake only."}

    async def _chat_with_tools(self, messages, tools, **kwargs):
        seen["tools"] = [t.name for t in tools]
        seen["messages"] = messages
        return seen["reply"]

    monkeypatch.setattr(LLMDispatchService, "chat_with_tools", _chat_with_tools)
    return seen


@pytest.fixture
def stub_history(monkeypatch):
    async def _build(dummy, session_key, **kwargs):
        return [{"role": "user", "content": "wake"}]

    monkeypatch.setattr(
        "server.services.prompt_assembler.build_chat_messages", _build)


async def _seed_email_thread(db) -> None:
    from server.services.email_store import EmailStore

    await db.execute(
        "INSERT INTO contacts (id, name, email, is_trusted, created_at, "
        "updated_at) VALUES ('c-jason', 'Jason Buisman', 'jason@example.test', "
        "1, datetime('now'), datetime('now'))")
    await db.execute(
        "INSERT INTO conversations (id, kind, created_at, updated_at) "
        "VALUES (?, 'dm', datetime('now'), datetime('now'))", (THREAD_KEY,))
    await db.execute(
        "INSERT INTO bindings (session_key, conversation_id, channel, kind, "
        "endpoint_kind, contact_id, is_active, created_at) VALUES "
        "(?, ?, 'email', 'thread', 'thread', 'c-jason', 1, datetime('now'))",
        (THREAD_KEY, THREAD_KEY))
    store = EmailStore(db)
    await store.insert_inbox(
        inbox_id="36bfb793-c7dc-4c3b-8421-4d2d458532e1",
        agentmail_inbox_id="inbox-am-1", display_name="Bob Jones",
        email_address="bob@example.test",
        metadata_json="{}", now_iso="2026-09-23T00:00:00+00:00")
    await db.execute(
        "INSERT INTO email_threads (id, inbox_id, agentmail_thread_id, "
        "subject, contact_id, session_key, message_count, is_active, "
        "created_at, updated_at) VALUES ('t-1', ?, '11111111-2222-3333-4444-"
        "555555555555', 'Smith gnome files', 'c-jason', ?, 0, 1, "
        "datetime('now'), datetime('now'))",
        ("36bfb793-c7dc-4c3b-8421-4d2d458532e1", THREAD_KEY))


async def test_wake_dispatches_email_thread_with_real_tools(
        ctx, db, stub_llm, stub_history):
    from server.services.wake_service import wake_conversation

    ctx.settings.openai.api_key = "sk-test"   # wake_thread's enabled guard
    await _seed_email_thread(db)
    dispatched = await wake_conversation(
        ctx, THREAD_KEY, "[Steering request — test]\ncheck the links",
        call_category="steer", provenance="steer")
    assert dispatched is True

    # give the fire-and-forget runner task a beat
    import asyncio

    for _ in range(50):
        await asyncio.sleep(0.05)
        if "tools" in stub_llm:
            break
    tools = stub_llm.get("tools") or []
    assert "email_reply" in tools, "email wake must carry the reply toolset"
    assert "bash" in tools                     # common tools present
    assert not any(t.startswith("mcp_") for t in tools)  # no servers attached

    row = await db.fetch_one(
        "SELECT dispatched FROM messages WHERE conversation_id = ? "
        "AND role = 'user'", (THREAD_KEY,))
    assert row and row["dispatched"] == 1


async def test_wake_unknown_email_thread_stores_undispatched(
        ctx, db, stub_llm, stub_history):
    from server.services.wake_service import wake_conversation

    missing = "agent:main:email:thread:99999999-9999-9999-9999-999999999999"
    dispatched = await wake_conversation(ctx, missing, "nope")
    assert dispatched is False
    row = await db.fetch_one(
        "SELECT dispatched FROM messages WHERE conversation_id = ? "
        "AND role = 'user'", (missing,))
    assert row and row["dispatched"] == 0      # stored for recovery


def test_dashboard_wake_endpoint_accepts_email_thread(
        tmp_path, monkeypatch, stub_llm, stub_history):
    """The `bob steer` surface: raw email session key resolves, passes the
    binding gate, and reports dispatched."""
    from fastapi.testclient import TestClient

    from server.config import OpenAISettings, Settings
    from server.main import create_app

    settings = Settings(
        data_dir=tmp_path / "data", config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "bob.db",
        openai=OpenAISettings(api_key="sk-test"))   # wake_thread's enabled guard
    app = create_app(settings)
    token = settings.resolved_api_secret

    with TestClient(app) as client:
        # app runs in its portal loop; seed via sync sqlite3 on the same
        # file (WAL allows the concurrent writer)
        import sqlite3

        conn = sqlite3.connect(settings.db_path)
        conn.execute(
            "INSERT INTO contacts (id, name, email, is_default, is_trusted, "
            "created_at, updated_at) VALUES ('c-mike', 'Mike Cleaver', "
            "'mike@example.test', 1, 1, datetime('now'), datetime('now'))")
        conn.execute(
            "INSERT INTO conversations (id, kind, created_at, updated_at) "
            "VALUES (?, 'dm', datetime('now'), datetime('now'))", (THREAD_KEY,))
        conn.execute(
            "INSERT INTO bindings (session_key, conversation_id, channel, kind, "
            "endpoint_kind, is_active, created_at) VALUES "
            "(?, ?, 'email', 'thread', 'thread', 1, datetime('now'))",
            (THREAD_KEY, THREAD_KEY))
        conn.execute(
            "INSERT INTO email_inboxes (id, agentmail_inbox_id, display_name, "
            "email_address, is_active, created_at, updated_at) VALUES "
            "('36bfb793-c7dc-4c3b-8421-4d2d458532e1', 'inbox-am-1', 'Bob', "
            "'bob@example.test', 1, datetime('now'), datetime('now'))")
        conn.execute(
            "INSERT INTO email_threads (id, inbox_id, agentmail_thread_id, "
            "subject, session_key, message_count, is_active, created_at, "
            "updated_at) VALUES ('t-1', '36bfb793-c7dc-4c3b-8421-4d2d458532e1', "
            "'11111111-2222-3333-4444-555555555555', 'Test thread', ?, 0, 1, "
            "datetime('now'), datetime('now'))", (THREAD_KEY,))
        conn.commit()
        conn.close()

        response = client.post(
            "/dashboard/api/conversations/wake",
            headers={"X-Dashboard-Secret": token},
            json={"target": THREAD_KEY,
                  "instruction": "verify the links are live"})
        body = response.json()
        assert body.get("ok") is True, body
        assert body.get("session_key") == THREAD_KEY
        assert body.get("dispatched") is True


async def test_email_attachment_cap_points_at_share_files(tmp_path, monkeypatch):
    """The 2026-09-22 dead-letter pair: a ~9 MB PNG and a 25 MB STL sailed
    past the old 25 MB guard and burned 5 retries each against AgentMail's
    ~10 MB request cap. The guard is now 8 MB/file + 11 MB combined, and
    the error routes Bob at the share-files skill."""
    from server.services.email_tools import (
        MAX_ATTACHMENT_SIZE, MAX_ATTACHMENT_TOTAL, _read_file_as_attachment,
    )

    assert MAX_ATTACHMENT_SIZE == 8 * 1024 * 1024
    assert MAX_ATTACHMENT_TOTAL == 11 * 1024 * 1024

    big = tmp_path / "model.stl"
    big.write_bytes(b"x" * (9 * 1024 * 1024))
    try:
        _read_file_as_attachment(str(big), tmp_path)
        raised = False
    except ValueError as exc:
        raised = True
        assert "8 MB" in str(exc)
        assert "share-files" in str(exc)      # the actionable path
    assert raised
