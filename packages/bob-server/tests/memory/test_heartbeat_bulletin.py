"""Test that the heartbeat bulletin-generation path produces plain-text bulletins."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from bob_server.services.memory.models import BulletinGeneratorInput


async def test_generate_session_bulletins_produces_texts(ctx, monkeypatch):
    """SessionIdleSummaryTask.run() should produce bulletins via generate_bulletins."""
    from bob_server.heartbeat import SessionIdleSummaryTask
    from bob_server.services.memory import bulletin_generator as bg

    now = datetime.now(timezone.utc)

    # Insert a contact
    await ctx.db.execute(
        "INSERT INTO contacts (id, name, phone_number, email, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "test12345-330b-4f15-bf2a-b1385a917677",
            "Blair Nicol",
            "+61401589328",
            "",
            now.isoformat(),
            now.isoformat(),
        ),
    )

    # Insert session messages far enough in the past to be "idle"
    old_enough = datetime(2000, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    await ctx.db.execute(
        "INSERT INTO session_messages (id, session_key, role, content, sender_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("msg-1", "sess-test", "user",
         "Let's go to Bali. We should look at flights and accommodation options for the trip.",
         "test12345-330b-4f15-bf2a-b1385a917677",
         old_enough.isoformat()),
    )

    # Insert a session participant
    await ctx.db.execute(
        "INSERT INTO session_participants (session_key, identifier, display_name, contact_id, is_trusted, last_active_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("sess-test", "blair", "Blair Nicol", "test12345-330b-4f15-bf2a-b1385a917677", 1, old_enough.isoformat()),
    )

    captured = {}

    async def fake_generate(llm, gen_input: BulletinGeneratorInput):
        captured["session_key"] = gen_input.session_key
        captured["participants"] = gen_input.participants
        return [
            "{{contact:test12345-330b-4f15-bf2a-b1385a917677|Blair Nicol}} wants to go to Bali (2026-06-01)",
        ]

    monkeypatch.setattr(bg, "generate_bulletins", fake_generate)

    task = SessionIdleSummaryTask()

    # Patch LLMDispatchService where it's defined so the local import in run() picks it up
    import bob_server.services.llm_dispatch as llm_mod

    mock_llm = AsyncMock()
    with patch.object(llm_mod, "LLMDispatchService", return_value=mock_llm):
        await task.run(ctx)

    assert captured.get("session_key") == "sess-test"
    assert len(captured.get("participants", [])) == 1


# ── Group entity linking tests ─────────────────────────────────────


async def test_ensure_group_entity_creates_entity(svc, db, workspace):
    """ensure_group_entity should create a group entity and link the bulletin."""
    now = datetime.now(timezone.utc).isoformat()
    session_key = "agent:main:whatsapp:group:12036342829458"
    chat_id = "12036342829458@g.us"

    # Set up session route and whatsappgroup
    await db.execute(
        "INSERT INTO session_routes (id, channel, session_key, kind, chat_id, created_at, updated_at) "
        "VALUES (?, 'whatsapp', ?, 'group', ?, ?, ?)",
        ("route-1", session_key, chat_id, now, now),
    )
    await db.execute(
        "INSERT INTO whatsappgroups (id, whatsapp_jid, name, description, created_at, updated_at) "
        "VALUES (?, ?, 'Bali Trip Chat', 'Planning our trip', ?, ?)",
        ("wg-1", chat_id, now, now),
    )

    # Write a bulletin
    bulletin_id = await svc.write_bulletin(
        workspace,
        channel_id="channel-whatsapp-group-12036342829458",
        source_type="session",
        source_id=session_key,
        content="Discussed Bali flights",
        visibility="group",
    )

    entity_id = await svc.ensure_group_entity(workspace, session_key, bulletin_id)

    assert entity_id is not None
    assert entity_id.startswith("group-")

    # Verify entity exists
    entity = await svc.read_entity(workspace, entity_id)
    assert entity is not None
    assert entity.entity_type == "group"
    assert entity.display_name == "Bali Trip Chat"

    # Verify whatsappgroups.memory_entity_id is set
    row = await db.fetch_one("SELECT memory_entity_id FROM whatsappgroups WHERE id = 'wg-1'")
    assert row["memory_entity_id"] == entity_id

    # Verify bulletin is linked
    link = await db.fetch_one(
        "SELECT 1 FROM memory_entity_bulletins WHERE entity_id = ? AND bulletin_id = ?",
        (entity_id, bulletin_id),
    )
    assert link is not None


async def test_ensure_group_entity_idempotent(svc, db, workspace):
    """Second call should reuse existing entity, not create a duplicate."""
    now = datetime.now(timezone.utc).isoformat()
    session_key = "agent:main:whatsapp:group:12036342829458"
    chat_id = "12036342829458@g.us"

    await db.execute(
        "INSERT INTO session_routes (id, channel, session_key, kind, chat_id, created_at, updated_at) "
        "VALUES (?, 'whatsapp', ?, 'group', ?, ?, ?)",
        ("route-1", session_key, chat_id, now, now),
    )
    await db.execute(
        "INSERT INTO whatsappgroups (id, whatsapp_jid, name, created_at, updated_at) "
        "VALUES (?, ?, 'Test Group', ?, ?)",
        ("wg-1", chat_id, now, now),
    )

    bulletin_id = await svc.write_bulletin(
        workspace, channel_id="ch", source_type="session",
        source_id=session_key, content="test",
    )

    entity_id_1 = await svc.ensure_group_entity(workspace, session_key, bulletin_id)
    entity_id_2 = await svc.ensure_group_entity(workspace, session_key, bulletin_id)

    assert entity_id_1 == entity_id_2

    # Only one group entity
    rows = await db.fetch_all("SELECT entity_id FROM memory_entities WHERE entity_type = 'group'")
    assert len(rows) == 1


async def test_ensure_group_entity_returns_none_for_dm(svc, db, workspace):
    """Should return None for non-group sessions."""
    now = datetime.now(timezone.utc).isoformat()
    session_key = "agent:main:whatsapp:dm:61456224867"

    # Create a contact so the FK constraint passes
    await db.execute(
        "INSERT INTO contacts (id, name, phone_number, created_at, updated_at) "
        "VALUES (?, 'Test', '+61456224867', ?, ?)",
        ("contact-1", now, now),
    )
    await db.execute(
        "INSERT INTO session_routes (id, channel, session_key, kind, contact_id, created_at, updated_at) "
        "VALUES (?, 'whatsapp', ?, 'dm', 'contact-1', ?, ?)",
        ("route-1", session_key, now, now),
    )

    result = await svc.ensure_group_entity(workspace, session_key, "bulletin-test")
    assert result is None


async def test_resolve_group_entity_id(svc, db):
    """_resolve_group_entity_id should look up via session_routes → whatsappgroups."""
    now = datetime.now(timezone.utc).isoformat()
    session_key = "agent:main:whatsapp:group:12036342829458"
    chat_id = "12036342829458@g.us"

    await db.execute(
        "INSERT INTO session_routes (id, channel, session_key, kind, chat_id, created_at, updated_at) "
        "VALUES (?, 'whatsapp', ?, 'group', ?, ?, ?)",
        ("route-1", session_key, chat_id, now, now),
    )
    await db.execute(
        "INSERT INTO whatsappgroups (id, whatsapp_jid, name, memory_entity_id, created_at, updated_at) "
        "VALUES (?, ?, 'Test', 'group-abc12345', ?, ?)",
        ("wg-1", chat_id, now, now),
    )

    result = await svc._resolve_group_entity_id(session_key)
    assert result == "group-abc12345"


async def test_resolve_group_entity_id_returns_none_when_unset(svc, db):
    """Should return None when memory_entity_id is not yet set."""
    now = datetime.now(timezone.utc).isoformat()
    session_key = "agent:main:whatsapp:group:12036342829458"
    chat_id = "12036342829458@g.us"

    await db.execute(
        "INSERT INTO session_routes (id, channel, session_key, kind, chat_id, created_at, updated_at) "
        "VALUES (?, 'whatsapp', ?, 'group', ?, ?, ?)",
        ("route-1", session_key, chat_id, now, now),
    )
    await db.execute(
        "INSERT INTO whatsappgroups (id, whatsapp_jid, name, created_at, updated_at) "
        "VALUES (?, ?, 'Test', ?, ?)",
        ("wg-1", chat_id, now, now),
    )

    result = await svc._resolve_group_entity_id(session_key)
    assert result is None
