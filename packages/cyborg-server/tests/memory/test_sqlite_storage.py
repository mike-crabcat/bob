"""Tests for SQLite-backed memory storage (v7 claim-centric model)."""

from __future__ import annotations

from datetime import datetime

import pytest

from cyborg_server.services.memory.models import (
    Claim,
    EntityDocument,
)


async def test_write_and_read_bulletin(svc, workspace):
    """Round-trip: write a bulletin, read it back."""
    bid = await svc.write_bulletin(
        workspace,
        channel_id="channel-whatsapp-group-123",
        source_type="session",
        source_id="session-abc",
        visibility="group",
        content="{{contact:7c9f0fd7|Blair}} decided to stay in Seminyak. {{contact:abc123|Michael}} will compare villas.",
    )

    assert bid.startswith("bulletin-")

    bulletins = await svc.read_bulletins(workspace)
    assert len(bulletins) == 1
    b = bulletins[0]
    assert b.channel_id == "channel-whatsapp-group-123"
    assert b.visibility == "group"
    assert "Seminyak" in b.content


async def test_bulletin_digested_filter(svc, workspace):
    """Undigested bulletins are returned by default; digested are filtered."""
    await svc.write_bulletin(
        workspace, channel_id="ch1", source_type="test", content="first",
    )
    await svc.write_bulletin(
        workspace, channel_id="ch2", source_type="test", content="second",
    )

    all_b = await svc.read_bulletins(workspace)
    assert len(all_b) == 2

    b = all_b[0]  # newest first
    await svc._mark_digested(b)

    pending = await svc.read_bulletins(workspace, skip_digested=True)
    assert len(pending) == 1
    assert pending[0].content == "first"


async def test_write_and_read_entity(svc, workspace):
    """Round-trip: write an entity record (identity only), read it back."""
    entity = EntityDocument(
        entity_id="trip-bali-2026",
        entity_type="trip",
        display_name="Bali 2026",
        status="active",
        source_bulletins=["bulletin-2026-06-01-abc123"],
    )
    eid = await svc.write_entity(workspace, entity)
    assert eid == "trip-bali-2026"

    result = await svc.read_entity(workspace, "trip-bali-2026")
    assert result is not None
    assert result.entity_id == "trip-bali-2026"
    assert result.entity_type == "trip"
    assert result.display_name == "Bali 2026"


async def test_aliases_write_through(svc, workspace):
    """Writing an entity creates alias entries."""
    entity = EntityDocument(
        entity_id="location-seminyak",
        entity_type="location",
        display_name="Seminyak",
    )
    await svc.write_entity(workspace, entity)

    rows = await svc.db.fetch_all(
        "SELECT * FROM memory_aliases WHERE entity_id = 'location-seminyak'"
    )
    aliases = {r["alias"] for r in rows}
    assert "Seminyak" in aliases
    assert "seminyak" in aliases


async def test_fk_cascade_delete_entity(svc, workspace):
    """Deleting an entity cascades to aliases."""
    entity = EntityDocument(
        entity_id="temp-entity",
        entity_type="file",
        display_name="Temp",
    )
    await svc.write_entity(workspace, entity)

    aliases = await svc.db.fetch_all(
        "SELECT * FROM memory_aliases WHERE entity_id = 'temp-entity'"
    )
    assert len(aliases) > 0

    await svc.db.execute("DELETE FROM memory_entities WHERE entity_id = 'temp-entity'")

    aliases = await svc.db.fetch_all(
        "SELECT * FROM memory_aliases WHERE entity_id = 'temp-entity'"
    )
    assert len(aliases) == 0


async def test_fk_cascade_delete_bulletin(svc, workspace):
    """Deleting a bulletin removes it from read results."""
    await svc.write_bulletin(
        workspace,
        channel_id="ch1",
        source_type="test",
        content="test bulletin to delete",
    )
    bulletins = await svc.read_bulletins(workspace)
    assert len(bulletins) == 1
    bid = bulletins[0].id

    await svc.db.execute("DELETE FROM memory_bulletins WHERE id = ?", (bid,))

    bulletins = await svc.read_bulletins(workspace)
    assert len(bulletins) == 0


async def test_claim_crud(svc, workspace):
    """Write and read claims via claim_service."""
    from cyborg_server.services.memory.claim_service import write_claim, read_claim, get_active_claims

    claim = Claim(
        id="claim-test-001",
        claim_type_key="destination",
        subject_id="trip-bali-2026",
        value="Seminyak, Bali",
        status="active",
        source_bulletins=["bulletin-2026-06-01-abc"],
        visibility="group",
        scope=["group-bali"],
        created_at=datetime.now(),
    )
    await write_claim(svc.db, claim)

    result = await read_claim(svc.db, "claim-test-001")
    assert result is not None
    assert result.claim_type_key == "destination"
    assert result.subject_id == "trip-bali-2026"
    assert result.value == "Seminyak, Bali"
    assert result.status == "active"

    active = await get_active_claims(svc.db, "trip-bali-2026")
    assert len(active) == 1

    claim.status = "superseded"
    await write_claim(svc.db, claim)
    active = await get_active_claims(svc.db, "trip-bali-2026")
    assert len(active) == 0


async def test_claim_entity_ref(svc, workspace):
    """Claims with object_id (entity references) work correctly."""
    from cyborg_server.services.memory.claim_service import write_claim, get_claims_by_type

    claim = Claim(
        id="claim-test-002",
        claim_type_key="spouse",
        subject_id="person-alice",
        object_id="person-bob",
        status="active",
        source_bulletins=["bulletin-test"],
        created_at=datetime.now(),
    )
    await write_claim(svc.db, claim)

    results = await get_claims_by_type(svc.db, "person-alice", "spouse")
    assert len(results) == 1
    assert results[0].object_id == "person-bob"


async def test_list_entities_by_type(svc, workspace):
    """list_entities returns only entities of the requested type."""
    await svc.write_entity(workspace, EntityDocument(
        entity_id="person-alice", entity_type="person", display_name="Alice",
    ))
    await svc.write_entity(workspace, EntityDocument(
        entity_id="trip-bali", entity_type="trip", display_name="Bali",
    ))

    persons = await svc.list_entities(workspace, "person")
    assert len(persons) == 1
    assert persons[0].entity_id == "person-alice"

    trips = await svc.list_entities(workspace, "trip")
    assert len(trips) == 1
    assert trips[0].entity_id == "trip-bali"


async def test_build_memory_index(svc, workspace):
    """build_memory_index produces expected format."""
    await svc.write_entity(workspace, EntityDocument(
        entity_id="person-alice", entity_type="person", display_name="Alice",
    ))
    await svc.write_entity(workspace, EntityDocument(
        entity_id="trip-bali", entity_type="trip", display_name="Bali 2026",
    ))

    index = await svc.build_memory_index(workspace)
    assert "Alice" in index
    assert "Bali 2026" in index
    assert "**person**" in index
    assert "**trip**" in index


async def test_check_constraints(db):
    """CHECK constraints reject invalid values."""
    with pytest.raises(Exception):
        await db.execute(
            "INSERT INTO memory_bulletins (id, created_at, channel_id, source_type, visibility, content) VALUES (?, ?, ?, ?, ?, ?)",
            ("test", "2026-01-01", "ch1", "test", "invalid", "content"),
        )

    with pytest.raises(Exception):
        await db.execute(
            "INSERT INTO memory_entities (entity_id, entity_type, display_name) VALUES (?, ?, '')",
            ("test", "invalid_type"),
        )


async def test_ensure_person_entry(svc, workspace):
    """ensure_person_entry creates a person entity if missing."""
    result = await svc.ensure_person_entry(
        workspace,
        contact_id="7c9f0fd7-6134-4495-aa8c-f04f11bc15e8",
        name="Blair Nicol",
        phone_number="+1234567890",
        email="blair@example.com",
        channel="whatsapp",
    )
    assert result == "person-blair-nicol"

    result2 = await svc.ensure_person_entry(
        workspace,
        contact_id="7c9f0fd7-6134-4495-aa8c-f04f11bc15e8",
        name="Blair Nicol",
    )
    assert result2 == "person-blair-nicol"


async def test_validate(svc, workspace):
    """validate checks for missing fields."""
    await svc.db.execute(
        "INSERT INTO memory_entities (entity_id, entity_type, display_name) VALUES (?, ?, '')",
        ("bad-entity", "person",),
    )
    result = await svc.validate(workspace)
    assert not result["valid"]
    assert any("bad-entity" in i for i in result["issues"])
