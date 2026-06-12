"""Tests for memory cleanup pipeline (renaming map, rewrites, run_cleanup).

In v7, entities have no body — cleanup rewrites claim references and deletes
duplicate entity records. No body merging needed.
"""

from __future__ import annotations

import pytest

from bob_server.services.memory.contact_directory import ContactDirectory, ContactRecord


async def _insert_contact_entity(db, entity_id: str, display_name: str) -> None:
    await db.execute(
        "INSERT INTO memory_entities (entity_id, entity_type, display_name, status) "
        "VALUES (?, 'person', ?, 'active')",
        (entity_id, display_name),
    )


async def test_renaming_map_matches_name_slug_to_canonical(db):
    from bob_server.services.memory.cleanup import build_renaming_map

    await _insert_contact_entity(db, "person-blair-nicol", "Blair Nicol")
    await _insert_contact_entity(db, "contact-blair-nicol", "Blair Nicol")
    await _insert_contact_entity(db, "unresolved-contact-gareth", "Gareth")

    directory = ContactDirectory([
        ContactRecord(
            uuid="03f3902d-330b-4f15-bf2a-b1385a917677",
            canonical_id="contact-03f3902d",
            name="Blair Nicol",
            phone_number="+61401589328",
            email="",
        ),
    ])

    rename, merge_into = await build_renaming_map(db, directory)

    assert rename.get("contact-blair-nicol") == "person-blair-nicol"
    assert merge_into.get("contact-blair-nicol") == "person-blair-nicol"
    assert "unresolved-contact-gareth" not in rename


async def test_renaming_map_handles_two_orphans_with_same_name(db):
    from bob_server.services.memory.cleanup import build_renaming_map

    await _insert_contact_entity(db, "bob-sr", "Bob Sr.")
    await _insert_contact_entity(db, "person-bob-sr", "Bob Sr.")

    rename, merge_into = await build_renaming_map(db, directory=None)

    assert rename.get("bob-sr") == "person-bob-sr"
    assert merge_into.get("bob-sr") == "person-bob-sr"


async def _insert_claim(db, claim_id: str, subject_id: str, object_id: str | None) -> None:
    await db.execute(
        "INSERT INTO memory_claims (id, claim_type_key, subject_id, object_id, status, "
        "source_bulletins, visibility, scope, created_at) "
        "VALUES (?, 'alias', ?, ?, 'active', '[]', 'private', '[]', '2026-01-01')",
        (claim_id, subject_id, object_id),
    )


async def test_rewrite_claims_renames_subject_and_object_ids(db):
    from bob_server.services.memory.cleanup import rewrite_claims

    await _insert_claim(db, "claim-001", "contact-blair-nicol", None)
    await _insert_claim(db, "claim-002", "contact-mike", "contact-blair-nicol")
    await _insert_claim(db, "claim-003", "trip-bali", "location-x")

    rename = {"contact-blair-nicol": "person-blair-nicol",
              "contact-mike": "person-mike"}
    count = await rewrite_claims(db, rename)

    assert count == 3
    c1 = await db.fetch_one("SELECT subject_id FROM memory_claims WHERE id = 'claim-001'")
    assert c1["subject_id"] == "person-blair-nicol"
    c2 = await db.fetch_one("SELECT subject_id, object_id FROM memory_claims WHERE id = 'claim-002'")
    assert c2["subject_id"] == "person-mike"
    assert c2["object_id"] == "person-blair-nicol"
    c3 = await db.fetch_one("SELECT subject_id FROM memory_claims WHERE id = 'claim-003'")
    assert c3["subject_id"] == "trip-bali"


async def test_rewrite_bulletin_entities_renames_contact_refs(db):
    from bob_server.services.memory.cleanup import rewrite_bulletin_entities

    await db.execute(
        "INSERT INTO memory_bulletins (id, channel_id, source_type, content, created_at) "
        "VALUES ('b-1', 'ch1', 'test', 'body1', '2026-01-01')",
    )
    await db.execute(
        "INSERT INTO memory_bulletins (id, channel_id, source_type, content, created_at) "
        "VALUES ('b-2', 'ch2', 'test', 'body2', '2026-01-01')",
    )
    await db.execute(
        "INSERT INTO memory_entity_bulletins (entity_id, bulletin_id) "
        "VALUES ('contact-blair-nicol', 'b-1')",
    )
    await db.execute(
        "INSERT INTO memory_entity_bulletins (entity_id, bulletin_id) "
        "VALUES ('contact-03f3902d', 'b-1')",
    )
    await db.execute(
        "INSERT INTO memory_entity_bulletins (entity_id, bulletin_id) "
        "VALUES ('contact-mike', 'b-2')",
    )

    rename = {"contact-blair-nicol": "person-blair-nicol",
              "contact-mike": "person-mike"}
    count = await rewrite_bulletin_entities(db, rename)

    assert count == 2
    refs = await db.fetch_all(
        "SELECT entity_id FROM memory_entity_bulletins WHERE bulletin_id = 'b-1' ORDER BY entity_id",
    )
    ids = [r["entity_id"] for r in refs]
    assert ids.count("person-blair-nicol") == 1
    refs2 = await db.fetch_all(
        "SELECT entity_id FROM memory_entity_bulletins WHERE bulletin_id = 'b-2'",
    )
    assert refs2[0]["entity_id"] == "person-mike"


async def test_rewrite_entity_relations_renames_contact_refs(db):
    from bob_server.services.memory.cleanup import rewrite_entity_relations

    await db.execute(
        "INSERT INTO memory_entities (entity_id, entity_type, display_name, status) "
        "VALUES ('trip-france', 'trip', 'France', 'active')",
    )
    await db.execute(
        "INSERT INTO memory_entity_relations (source_entity_id, category, target_entity_id) "
        "VALUES ('trip-france', 'contacts', 'contact-blair-nicol')",
    )
    await db.execute(
        "INSERT INTO memory_entity_relations (source_entity_id, category, target_entity_id) "
        "VALUES ('trip-france', 'contacts', 'contact-mike')",
    )

    rename = {"contact-blair-nicol": "person-blair-nicol",
              "contact-mike": "person-mike"}
    count = await rewrite_entity_relations(db, rename)

    assert count == 2
    rels = await db.fetch_all(
        "SELECT target_entity_id FROM memory_entity_relations WHERE source_entity_id = 'trip-france'",
    )
    targets = {r["target_entity_id"] for r in rels}
    assert targets == {"person-blair-nicol", "person-mike"}


async def test_run_cleanup_end_to_end(db):
    from bob_server.services.memory.cleanup import run_cleanup

    await _insert_contact_entity(db, "person-blair-nicol", "Blair Nicol")
    await _insert_contact_entity(db, "contact-blair-nicol", "Blair Nicol")
    await _insert_contact_entity(db, "bob-sr", "Bob Sr.")
    await _insert_contact_entity(db, "person-bob-sr", "Bob Sr.")
    await _insert_claim(db, "claim-001", "contact-blair-nicol", "bob-sr")
    await db.execute(
        "INSERT INTO memory_bulletins (id, channel_id, source_type, content, created_at) "
        "VALUES ('b-1', 'ch1', 'test', 'body', '2026-01-01')",
    )
    await db.execute(
        "INSERT INTO memory_entity_bulletins (entity_id, bulletin_id) "
        "VALUES ('contact-blair-nicol', 'b-1')",
    )

    directory = ContactDirectory([
        ContactRecord("03f3902d-330b-4f15-bf2a-b1385a917677", "contact-03f3902d",
                      "Blair Nicol", "+61401589328", ""),
    ])

    summary = await run_cleanup(db, directory, dry_run=False)

    # Canonical person-blair-nicol still exists, contact-blair-nicol deleted
    canon = await db.fetch_one("SELECT * FROM memory_entities WHERE entity_id = 'person-blair-nicol'")
    assert canon is not None
    dup = await db.fetch_one("SELECT * FROM memory_entities WHERE entity_id = 'contact-blair-nicol'")
    assert dup is None
    # Bob Sr winner kept, loser deleted
    winner = await db.fetch_one("SELECT * FROM memory_entities WHERE entity_id = 'person-bob-sr'")
    assert winner is not None
    loser = await db.fetch_one("SELECT * FROM memory_entities WHERE entity_id = 'bob-sr'")
    assert loser is None
    # Claim rewritten
    c1 = await db.fetch_one("SELECT subject_id, object_id FROM memory_claims WHERE id = 'claim-001'")
    assert c1["subject_id"] == "person-blair-nicol"
    assert c1["object_id"] == "person-bob-sr"
    # Bulletin entity ref rewritten
    brefs = await db.fetch_all(
        "SELECT entity_id FROM memory_entity_bulletins WHERE bulletin_id = 'b-1'",
    )
    assert brefs[0]["entity_id"] == "person-blair-nicol"

    assert summary["rewritten_claims"] >= 1
    assert summary["rewritten_bulletins"] >= 1
