"""Conversations & bindings tests (Bob3 Phase VI items 1-2, 4).

Merge/unmerge fixture suite: provenance, pre-merge event ownership via
bindings, post-merge artifacts staying with the survivor.
"""

from __future__ import annotations

import pytest

from bob_server.repositories.conversations import ConversationRepository


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
