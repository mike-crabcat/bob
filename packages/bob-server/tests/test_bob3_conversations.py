"""Conversations & bindings tests (Bob3 Phase VI items 1-2, 4).

Merge/unmerge fixture suite: provenance, pre-merge event ownership via
bindings, post-merge artifacts staying with the survivor.
"""

from __future__ import annotations

import pytest

from bob_server.repositories.conversations import ConversationRepository
from tests.services.test_whatsapp_inbound_characterization import (  # noqa: F401
    immediate_patience,
    stub_memory,
)


async def test_backfill_created_conversations_for_existing_sessions(ctx, db):
    """Simulate the migration's lazy path: ensure() creates the 1:1
    conversation + binding with id == session_key."""
    repo = ConversationRepository(db)
    conv = await repo.ensure("agent:main:whatsapp:dm:123")
    assert conv["id"] == "agent:main:whatsapp:dm:123"
    assert conv["kind"] == "dm"
    binding = await repo.get_binding("agent:main:whatsapp:dm:123")
    assert binding["channel"] == "whatsapp" and binding["kind"] == "thread"

    group = await repo.ensure("agent:main:whatsapp:group:99")
    assert group["kind"] == "group"
    email = await repo.ensure("agent:main:email:dm:a@b.c")
    binding = await repo.get_binding("agent:main:email:dm:a@b.c")
    assert binding["channel"] == "email"
    assert email["kind"] == "dm"


async def test_ensure_is_idempotent(ctx, db):
    repo = ConversationRepository(db)
    a = await repo.ensure("agent:main:whatsapp:dm:1")
    b = await repo.ensure("agent:main:whatsapp:dm:1")
    assert a["id"] == b["id"]
    rows = await db.fetch_all("SELECT * FROM conversations WHERE id = ?", (a["id"],))
    assert len(rows) == 1


async def test_merge_moves_bindings_with_provenance(ctx, db):
    """A person's WhatsApp DM and email thread become one conversation with
    two bindings; provenance recorded; merged conversation marked."""
    repo = ConversationRepository(db)
    wa = await repo.ensure("agent:main:whatsapp:dm:555")
    em = await repo.ensure("agent:main:email:dm:sarah@x.com")

    moved = await repo.merge([em["id"]], wa["id"], note="same person: Sarah")
    assert moved == 1

    resolved = await repo.resolve("agent:main:email:dm:sarah@x.com")
    assert resolved["id"] == wa["id"], "email session now resolves to the survivor"
    binding = await repo.get_binding("agent:main:email:dm:sarah@x.com")
    assert binding["merged_from"] == em["id"] and binding["merged_at"]

    merged_row = await repo.get(em["id"])
    assert merged_row["merged_into"] == wa["id"]
    bindings = await repo.bindings_for(wa["id"])
    assert {b["channel"] for b in bindings} == {"whatsapp", "email"}

    ev = await db.fetch_one(
        "SELECT * FROM event_log WHERE event_type = 'conversation.merged'")
    assert ev is not None


async def test_merge_into_merged_survivor_rejected(ctx, db):
    repo = ConversationRepository(db)
    a = await repo.ensure("agent:main:whatsapp:dm:1")
    b = await repo.ensure("agent:main:whatsapp:dm:2")
    c = await repo.ensure("agent:main:whatsapp:dm:3")
    await repo.merge([a["id"]], b["id"])
    with pytest.raises(ValueError):
        await repo.merge([c["id"]], a["id"])  # a was merged away


async def test_unmerge_restores_binding_to_original(ctx, db):
    repo = ConversationRepository(db)
    wa = await repo.ensure("agent:main:whatsapp:dm:7")
    em = await repo.ensure("agent:main:email:dm:j@x.com")
    await repo.merge([em["id"]], wa["id"])

    restored = await repo.unmerge("agent:main:email:dm:j@x.com")
    assert restored == em["id"]
    resolved = await repo.resolve("agent:main:email:dm:j@x.com")
    assert resolved["id"] == em["id"]
    assert resolved["merged_into"] is None, "original conversation reactivated"
    binding = await repo.get_binding("agent:main:email:dm:j@x.com")
    assert binding["merged_from"] is None and binding["merged_at"] is None

    # Survivor keeps its own binding untouched (post-merge artifacts stay).
    still = await repo.resolve("agent:main:whatsapp:dm:7")
    assert still["id"] == wa["id"]


async def test_unmerge_of_unmerged_binding_is_noop(ctx, db):
    repo = ConversationRepository(db)
    await repo.ensure("agent:main:whatsapp:dm:8")
    assert await repo.unmerge("agent:main:whatsapp:dm:8") is None
    assert await repo.unmerge("never-existed") is None


async def test_premerge_events_follow_binding_on_unmerge(ctx, db):
    """Events are keyed by binding (invariant 3): after unmerge, querying by
    the restored conversation_id returns the pre-merge events unchanged."""
    from bob_server.repositories.event_log import Event, EventLogRepository

    repo = ConversationRepository(db)
    em = await repo.ensure("agent:main:email:dm:k@x.com")
    wa = await repo.ensure("agent:main:whatsapp:dm:9")

    events = EventLogRepository(db)
    await events.append(Event(
        event_type="message.received",
        binding_key="agent:main:email:dm:k@x.com",
        conversation_id=em["id"],
        source="email", external_id="pre-merge-1", payload={"text": "hi"}))

    await repo.merge([em["id"]], wa["id"])
    await repo.unmerge("agent:main:email:dm:k@x.com")

    row = await db.fetch_one(
        "SELECT * FROM event_log WHERE external_id = 'pre-merge-1'")
    assert row["conversation_id"] == em["id"], "pre-merge event still owned by original"


# ---------------------------------------------------- ingress canonicalization


async def test_inbound_on_merged_binding_lands_in_survivor(
        ctx, tmp_path, immediate_patience, stub_memory, monkeypatch):
    """Phase VI item 3 exit fixture: after merging B into A, a new inbound
    message on B's channel binding must key ALL downstream state under A —
    session_messages, event conversation_id — while the event's binding_key
    preserves the original channel address."""
    from tests.services.test_whatsapp_inbound_characterization import (
        _dm_payload,
        _make_service,
        _seed_contact,
        _stub_llm,
        _stub_workspace,
    )

    _stub_workspace(monkeypatch)

    async def behaviour(messages, tools):
        return ""
    _stub_llm(monkeypatch, behaviour)

    await _seed_contact(ctx.db, "+61400000001", trusted=1)
    await _seed_contact(ctx.db, "+61400000002", trusted=1)
    key_a = "agent:main:whatsapp:dm:61400000001"
    key_b = "agent:main:whatsapp:dm:61400000002"

    repo = ConversationRepository(ctx.db)
    await repo.ensure(key_a)
    await repo.ensure(key_b)
    await repo.merge([key_b], key_a, note="same person")

    svc = _make_service(ctx, tmp_path)
    await svc._handle_incoming_message(_dm_payload("+61400000002", "hello from B", "wamid-merged-1"))

    msgs = await ctx.db.fetch_all(
        "SELECT session_key FROM session_messages WHERE role='user' AND content LIKE '%hello from B%'")
    assert msgs and all(m["session_key"] == key_a for m in msgs), \
        "merged binding's messages must key under the survivor conversation"

    ev = await ctx.db.fetch_one(
        "SELECT binding_key, conversation_id FROM event_log WHERE external_id = 'wamid-merged-1'")
    assert ev is not None
    assert ev["conversation_id"] == key_a
    assert ev["binding_key"] == key_b, "binding_key preserves the channel address"


async def test_inbound_on_unmerged_binding_unchanged(
        ctx, tmp_path, immediate_patience, stub_memory, monkeypatch):
    """1:1 case (all production traffic today): canonical id == session_key,
    behaviour identical to pre-canonicalization."""
    from tests.services.test_whatsapp_inbound_characterization import (
        _dm_payload,
        _make_service,
        _seed_contact,
        _stub_llm,
        _stub_workspace,
    )

    _stub_workspace(monkeypatch)

    async def behaviour(messages, tools):
        return ""
    _stub_llm(monkeypatch, behaviour)

    await _seed_contact(ctx.db, "+61400000003", trusted=1)
    key = "agent:main:whatsapp:dm:61400000003"

    svc = _make_service(ctx, tmp_path)
    await svc._handle_incoming_message(_dm_payload("+61400000003", "plain hello", "wamid-plain-1"))

    ev = await ctx.db.fetch_one(
        "SELECT binding_key, conversation_id FROM event_log WHERE external_id = 'wamid-plain-1'")
    assert ev["binding_key"] == key and ev["conversation_id"] == key


async def test_wake_channel_resolution_uses_bindings_after_merge(ctx, db):
    """Outbound seam: a survivor whose id is not channel-shaped resolves its
    channel via the binding map (prefer WhatsApp)."""
    from bob_server.services.wake_service import conversation_channel

    repo = ConversationRepository(db)
    # Channel-shaped id: key parsing wins, no lookup needed.
    await repo.ensure("agent:main:whatsapp:dm:777")
    channel, key = await conversation_channel(ctx, "agent:main:whatsapp:dm:777")
    assert channel == "whatsapp" and key == "agent:main:whatsapp:dm:777"

    # Non-channel-shaped conversation with a WA binding attached via merge.
    now = "2026-01-01T00:00:00+00:00"
    await db.execute(
        "INSERT INTO conversations (id, kind, created_at, updated_at) VALUES (?, 'dm', ?, ?)",
        ("person-merged-1", now, now))
    await repo.ensure("agent:main:whatsapp:dm:888")
    await repo.ensure("agent:main:email:thread:t-888")
    await repo.merge(["agent:main:whatsapp:dm:888", "agent:main:email:thread:t-888"],
                     "person-merged-1")
    channel, key = await conversation_channel(ctx, "person-merged-1")
    assert channel == "whatsapp" and key == "agent:main:whatsapp:dm:888"
