"""Tests for SQLite-backed memory storage."""

from __future__ import annotations

from datetime import datetime

import pytest

from cyborg_server.services.memory.models import (
    Claim,
    EntityDocument,
)


async def test_write_and_read_bulletin(svc, workspace):
    """Round-trip: write a bulletin with all typed fields, read it back."""
    bid = await svc.write_bulletin(
        workspace,
        channel_id="channel-whatsapp-group-123",
        source_type="session_transcript_range",
        source_id="session-abc",
        session_id="agent:main:whatsapp:group:123",
        transcript_range_id="range-202606011000",
        visibility="group",
        scope=["group-bali-travellers"],
        entities={
            "contacts": [
                {"id": "contact-7c9f0fd7", "display_name": "Blair Nicol", "resolution_status": "known"},
            ],
            "channels": [{"id": "channel-whatsapp-group-123"}],
        },
        memory_types=["decision", "task"],
        confidence="high",
        requires_review=True,
        review_reasons=["contains booking details"],
        content="Group decided to stay in Seminyak. Michael will compare villas.",
    )

    assert bid.startswith("bulletin-")

    bulletins = await svc.read_bulletins(workspace)
    assert len(bulletins) == 1
    b = bulletins[0]
    assert b.channel_id == "channel-whatsapp-group-123"
    assert b.visibility == "group"
    assert b.scope == ["group-bali-travellers"]
    assert "contact-7c9f0fd7" in [r.id for r in b.entities.get("contacts", [])]
    assert b.content == "Group decided to stay in Seminyak. Michael will compare villas."


async def test_bulletin_digested_filter(svc, workspace):
    """Undigested bulletins are returned by default; digested are filtered."""
    await svc.write_bulletin(
        workspace, channel_id="ch1", source_type="test", content="first",
    )
    await svc.write_bulletin(
        workspace, channel_id="ch2", source_type="test", content="second",
    )

    # Both undigested
    all_b = await svc.read_bulletins(workspace)
    assert len(all_b) == 2

    # Mark "second" (newest, index 0) as digested
    b = all_b[0]  # newest first
    await svc._mark_digested(b)

    pending = await svc.read_bulletins(workspace, skip_digested=True)
    assert len(pending) == 1
    assert pending[0].content == "first"


async def test_write_and_read_entity(svc, workspace):
    """Round-trip: write an entity, read it back with relations."""
    entity = EntityDocument(
        entity_id="trip-bali-2026",
        entity_type="trip",
        display_name="Bali 2026",
        status="active",
        body="# Bali 2026\n\n## Summary\n\nGroup trip to Bali.\n\n## Related Entities\n\ncontacts: []\ntrips: []\n",
        source_bulletins=["bulletin-2026-06-01-abc123"],
    )
    eid = await svc.write_entity(workspace, entity)
    assert eid == "trip-bali-2026"

    result = await svc.read_entity(workspace, "trip-bali-2026")
    assert result is not None
    assert result.entity_id == "trip-bali-2026"
    assert result.entity_type == "trip"
    assert result.display_name == "Bali 2026"
    assert result.source_bulletins == ["bulletin-2026-06-01-abc123"]


async def test_entity_relations(svc, workspace):
    """Relations are stored in junction table and recovered on read."""
    entity = EntityDocument(
        entity_id="trip-bali-2026",
        entity_type="trip",
        display_name="Bali 2026",
        body="## Summary\n\nTrip.\n\n## Related Entities\n\ncontacts: [contact-abc]\nlocations: [location-seminyak]\n",
        related_entities={
            "contacts": ["contact-abc"],
            "locations": ["location-seminyak"],
        },
    )
    await svc.write_entity(workspace, entity)

    # Verify relations in junction table
    rows = await svc.db.fetch_all(
        "SELECT * FROM memory_entity_relations WHERE source_entity_id = 'trip-bali-2026'"
    )
    assert len(rows) == 2
    cats = {r["category"]: r["target_entity_id"] for r in rows}
    assert cats["contacts"] == "contact-abc"
    assert cats["locations"] == "location-seminyak"

    # Verify read_entity populates related_entities
    result = await svc.read_entity(workspace, "trip-bali-2026")
    assert "contact-abc" in result.related_entities.get("contacts", [])
    assert "location-seminyak" in result.related_entities.get("locations", [])


async def test_aliases_write_through(svc, workspace):
    """Writing an entity creates alias entries."""
    entity = EntityDocument(
        entity_id="location-seminyak",
        entity_type="location",
        display_name="Seminyak",
        body="# Seminyak\n\nBeach town in Bali.",
    )
    await svc.write_entity(workspace, entity)

    rows = await svc.db.fetch_all(
        "SELECT * FROM memory_aliases WHERE entity_id = 'location-seminyak'"
    )
    aliases = {r["alias"] for r in rows}
    assert "Seminyak" in aliases
    assert "seminyak" in aliases


async def test_fk_cascade_delete_entity(svc, workspace):
    """Deleting an entity cascades to relations and aliases."""
    entity = EntityDocument(
        entity_id="temp-entity",
        entity_type="artifact",
        display_name="Temp",
        body="Temporary",
        related_entities={"contacts": ["contact-abc"]},
    )
    await svc.write_entity(workspace, entity)

    # Verify rows exist
    rels = await svc.db.fetch_all(
        "SELECT * FROM memory_entity_relations WHERE source_entity_id = 'temp-entity'"
    )
    assert len(rels) == 1
    aliases = await svc.db.fetch_all(
        "SELECT * FROM memory_aliases WHERE entity_id = 'temp-entity'"
    )
    assert len(aliases) > 0

    # Delete
    await svc.db.execute("DELETE FROM memory_entities WHERE entity_id = 'temp-entity'")

    # Verify cascade
    rels = await svc.db.fetch_all(
        "SELECT * FROM memory_entity_relations WHERE source_entity_id = 'temp-entity'"
    )
    assert len(rels) == 0
    aliases = await svc.db.fetch_all(
        "SELECT * FROM memory_aliases WHERE entity_id = 'temp-entity'"
    )
    assert len(aliases) == 0


async def test_fk_cascade_delete_bulletin(svc, workspace):
    """Deleting a bulletin cascades to entity refs."""
    await svc.write_bulletin(
        workspace,
        channel_id="ch1",
        source_type="test",
        content="test",
        entities={"contacts": [{"id": "contact-abc"}]},
    )
    bulletins = await svc.read_bulletins(workspace)
    bid = bulletins[0].id

    # Verify entity ref exists
    refs = await svc.db.fetch_all(
        "SELECT * FROM memory_bulletin_entities WHERE bulletin_id = ?", (bid,)
    )
    assert len(refs) == 1

    # Delete
    await svc.db.execute("DELETE FROM memory_bulletins WHERE id = ?", (bid,))

    # Verify cascade
    refs = await svc.db.fetch_all(
        "SELECT * FROM memory_bulletin_entities WHERE bulletin_id = ?", (bid,)
    )
    assert len(refs) == 0


async def test_claim_crud(svc, workspace):
    """Write and read claims via claim_service."""
    from cyborg_server.services.memory.claim_service import write_claim, read_claim, get_active_claims

    claim = Claim(
        id="claim-test-001",
        type="fact",
        subject_id="trip-bali-2026",
        predicate="preferred_location",
        object_id="location-seminyak",
        status="active",
        source_bulletins=["bulletin-2026-06-01-abc"],
        visibility="group",
        scope=["group-bali"],
        created_at=datetime.now(),
        body="Bali 2026 prefers Seminyak for accommodation.",
    )
    await write_claim(svc.db, claim)

    result = await read_claim(svc.db, "claim-test-001")
    assert result is not None
    assert result.type == "fact"
    assert result.subject_id == "trip-bali-2026"
    assert result.object_id == "location-seminyak"
    assert result.status == "active"

    # Get active claims for entity
    active = await get_active_claims(svc.db, "trip-bali-2026")
    assert len(active) == 1

    # Supersede the claim
    claim.status = "superseded"
    await write_claim(svc.db, claim)
    active = await get_active_claims(svc.db, "trip-bali-2026")
    assert len(active) == 0


async def test_list_entities_by_type(svc, workspace):
    """list_entities returns only entities of the requested type."""
    await svc.write_entity(workspace, EntityDocument(
        entity_id="contact-abc", entity_type="contact", display_name="Alice", body="Alice",
    ))
    await svc.write_entity(workspace, EntityDocument(
        entity_id="trip-bali", entity_type="trip", display_name="Bali", body="Bali trip",
    ))

    contacts = await svc.list_entities(workspace, "contact")
    assert len(contacts) == 1
    assert contacts[0].entity_id == "contact-abc"

    trips = await svc.list_entities(workspace, "trip")
    assert len(trips) == 1
    assert trips[0].entity_id == "trip-bali"


async def test_build_memory_index(svc, workspace):
    """build_memory_index produces expected format."""
    await svc.write_entity(workspace, EntityDocument(
        entity_id="contact-abc", entity_type="contact", display_name="Alice",
        body="# Alice\n\nA software engineer.",
    ))
    await svc.write_entity(workspace, EntityDocument(
        entity_id="trip-bali", entity_type="trip", display_name="Bali 2026",
        body="# Bali\n\nGroup trip to Bali in June 2026.",
    ))

    index = await svc.build_memory_index(workspace)
    assert "Alice" in index
    assert "Bali 2026" in index
    assert "**contact**" in index
    assert "**trip**" in index


async def test_check_constraints(db):
    """CHECK constraints reject invalid values."""
    with pytest.raises(Exception):
        await db.execute(
            "INSERT INTO memory_bulletins (id, created_at, visibility) VALUES (?, ?, ?)",
            ("test", "2026-01-01", "invalid"),
        )

    with pytest.raises(Exception):
        await db.execute(
            "INSERT INTO memory_claims (id, type, subject_id, created_at, status) VALUES (?, ?, ?, ?, ?)",
            ("test", "invalid_type", "sub1", "2026-01-01", "active"),
        )

    with pytest.raises(Exception):
        await db.execute(
            "INSERT INTO memory_entities (entity_id, entity_type) VALUES (?, ?)",
            ("test", "invalid_type"),
        )


async def test_ensure_person_entry(svc, workspace):
    """ensure_person_entry creates a contact entity if missing."""
    result = await svc.ensure_person_entry(
        workspace,
        contact_id="7c9f0fd7-6134-4495-aa8c-f04f11bc15e8",
        name="Blair Nicol",
        phone_number="+1234567890",
        email="blair@example.com",
        channel="whatsapp",
    )
    assert result == "contact-7c9f0fd7"

    # Second call returns existing
    result2 = await svc.ensure_person_entry(
        workspace,
        contact_id="7c9f0fd7-6134-4495-aa8c-f04f11bc15e8",
        name="Blair Nicol",
    )
    assert result2 == "contact-7c9f0fd7"


async def test_validate(svc, workspace):
    """validate checks for missing fields."""
    # Insert entity with empty display_name
    await svc.db.execute(
        "INSERT INTO memory_entities (entity_id, entity_type, display_name) VALUES (?, ?, '')",
        ("bad-entity", "contact",),
    )
    result = await svc.validate(workspace)
    assert not result["valid"]
    assert any("bad-entity" in i for i in result["issues"])
