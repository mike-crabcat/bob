"""Tests for memory cleanup pipeline (merge_entity_docs, build_renaming_map, rewrites, run_cleanup)."""

from __future__ import annotations

import json

import pytest

from cyborg_server.services.memory.contact_directory import ContactDirectory, ContactRecord
from cyborg_server.services.memory.models import EntityDocument


def test_merge_combines_timelines_and_source_bulletins():
    from cyborg_server.services.memory.cleanup import merge_entity_docs

    canonical = EntityDocument(
        entity_id="contact-03f3902d",
        entity_type="contact",
        display_name="Blair Nicol",
        status="active",
        extra_frontmatter={},
        body=(
            "## Summary\n\nLikes beer.\n\n"
            "## Timeline\n\n- 2026-05-30: Asked about beer.\n\n"
            "## Source Bulletins\n\n- bulletin-aaa\n"
        ),
    )
    dup = EntityDocument(
        entity_id="contact-blair-nicol",
        entity_type="contact",
        display_name="Blair Nicol",
        status="active",
        extra_frontmatter={},
        body=(
            "## Summary\n\nFrench, married to Katie.\n\n"
            "## Timeline\n\n- 2026-05-31: Mentioned being French.\n\n"
            "## Source Bulletins\n\n- bulletin-bbb\n"
        ),
    )
    merged = merge_entity_docs(canonical, dup)
    assert merged.entity_id == "contact-03f3902d"
    assert "Likes beer." in merged.body
    assert "French, married to Katie." in merged.body
    assert "2026-05-30: Asked about beer." in merged.body
    assert "2026-05-31: Mentioned being French." in merged.body
    assert "bulletin-aaa" in merged.body
    assert "bulletin-bbb" in merged.body


def test_merge_preserves_canonical_display_name():
    from cyborg_server.services.memory.cleanup import merge_entity_docs

    canonical = EntityDocument(
        entity_id="contact-fa10577d", entity_type="contact",
        display_name="Gareth Parry", status="active", extra_frontmatter={},
        body="## Summary\n\nA.\n",
    )
    dup = EntityDocument(
        entity_id="unresolved-contact-gareth", entity_type="contact",
        display_name="Gareth", status="active", extra_frontmatter={},
        body="## Summary\n\nB.\n",
    )
    merged = merge_entity_docs(canonical, dup)
    assert merged.display_name == "Gareth Parry"


def test_merge_related_entities_deduped():
    from cyborg_server.services.memory.cleanup import merge_entity_docs

    canonical = EntityDocument(
        entity_id="contact-03f3902d", entity_type="contact",
        display_name="Blair Nicol", status="active", extra_frontmatter={},
        body=(
            "## Related Entities\n\n"
            "contacts: []\ngroups: []\nchannels:\n  - channel-x\n"
            "trips: []\nlocations: []\nevents: []\ntasks: []\nartifacts: []\ndecisions: []\n"
        ),
    )
    dup = EntityDocument(
        entity_id="contact-blair-nicol", entity_type="contact",
        display_name="Blair Nicol", status="active", extra_frontmatter={},
        body=(
            "## Related Entities\n\n"
            "contacts:\n  - contact-mike\n"
            "groups: []\nchannels:\n  - channel-x\n  - channel-y\n"
            "trips: []\nlocations: []\nevents: []\ntasks: []\nartifacts: []\ndecisions: []\n"
        ),
    )
    merged = merge_entity_docs(canonical, dup)
    assert merged.body.count("channel-x") == 1
    assert "channel-y" in merged.body
    assert "contact-mike" in merged.body


async def _insert_contact_entity(db, entity_id: str, display_name: str) -> None:
    await db.execute(
        "INSERT INTO memory_entities (entity_id, entity_type, display_name, status, body) "
        "VALUES (?, 'contact', ?, 'active', ?)",
        (entity_id, display_name, f"# {display_name}\n"),
    )


async def test_renaming_map_matches_name_slug_to_canonical(db):
    from cyborg_server.services.memory.cleanup import build_renaming_map

    await _insert_contact_entity(db, "contact-03f3902d", "Blair Nicol")
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

    assert rename.get("contact-blair-nicol") == "contact-03f3902d"
    assert merge_into.get("contact-blair-nicol") == "contact-03f3902d"
    assert "unresolved-contact-gareth" not in rename


async def test_renaming_map_handles_two_orphans_with_same_name(db):
    from cyborg_server.services.memory.cleanup import build_renaming_map

    await _insert_contact_entity(db, "bob-sr", "Bob Sr.")
    await _insert_contact_entity(db, "contact-bob-sr", "Bob Sr.")

    rename, merge_into = await build_renaming_map(db, directory=None)

    assert rename.get("bob-sr") == "contact-bob-sr"
    assert merge_into.get("bob-sr") == "contact-bob-sr"


async def _insert_claim(db, claim_id: str, subject_id: str, object_id: str | None) -> None:
    await db.execute(
        "INSERT INTO memory_claims (id, type, subject_id, predicate, object_id, status, "
        "source_bulletins, visibility, scope, created_at) "
        "VALUES (?, 'fact', ?, 'x', ?, 'active', '[]', 'private', '[]', '2026-01-01')",
        (claim_id, subject_id, object_id),
    )


async def test_rewrite_claims_renames_subject_and_object_ids(db):
    from cyborg_server.services.memory.cleanup import rewrite_claims

    await _insert_claim(db, "claim-001", "contact-blair-nicol", None)
    await _insert_claim(db, "claim-002", "contact-mike", "contact-blair-nicol")
    await _insert_claim(db, "claim-003", "trip-bali", "location-x")

    rename = {"contact-blair-nicol": "contact-03f3902d",
              "contact-mike": "contact-7c9f0fd7"}
    count = await rewrite_claims(db, rename)

    assert count == 3
    c1 = await db.fetch_one("SELECT subject_id FROM memory_claims WHERE id = 'claim-001'")
    assert c1["subject_id"] == "contact-03f3902d"
    c2 = await db.fetch_one("SELECT subject_id, object_id FROM memory_claims WHERE id = 'claim-002'")
    assert c2["subject_id"] == "contact-7c9f0fd7"
    assert c2["object_id"] == "contact-03f3902d"
    c3 = await db.fetch_one("SELECT subject_id FROM memory_claims WHERE id = 'claim-003'")
    assert c3["subject_id"] == "trip-bali"


async def test_rewrite_bulletin_entities_renames_contact_refs(db):
    from cyborg_server.services.memory.cleanup import rewrite_bulletin_entities

    # Insert bulletins
    await db.execute(
        "INSERT INTO memory_bulletins (id, channel_id, source_type, content, created_at) "
        "VALUES ('b-1', 'ch1', 'test', 'body1', '2026-01-01')",
    )
    await db.execute(
        "INSERT INTO memory_bulletins (id, channel_id, source_type, content, created_at) "
        "VALUES ('b-2', 'ch2', 'test', 'body2', '2026-01-01')",
    )
    # Insert entity refs
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

    rename = {"contact-blair-nicol": "contact-03f3902d",
              "contact-mike": "contact-7c9f0fd7"}
    count = await rewrite_bulletin_entities(db, rename)

    assert count == 2
    refs = await db.fetch_all(
        "SELECT entity_id FROM memory_entity_bulletins WHERE bulletin_id = 'b-1' ORDER BY entity_id",
    )
    ids = [r["entity_id"] for r in refs]
    assert ids.count("contact-03f3902d") == 1
    refs2 = await db.fetch_all(
        "SELECT entity_id FROM memory_entity_bulletins WHERE bulletin_id = 'b-2'",
    )
    assert refs2[0]["entity_id"] == "contact-7c9f0fd7"


async def test_rewrite_entity_relations_renames_contact_refs(db):
    from cyborg_server.services.memory.cleanup import rewrite_entity_relations

    # Insert a trip entity
    await db.execute(
        "INSERT INTO memory_entities (entity_id, entity_type, display_name, status, body) "
        "VALUES ('trip-france', 'trip', 'France', 'active', '')",
    )
    # Insert relations
    await db.execute(
        "INSERT INTO memory_entity_relations (source_entity_id, category, target_entity_id) "
        "VALUES ('trip-france', 'contacts', 'contact-blair-nicol')",
    )
    await db.execute(
        "INSERT INTO memory_entity_relations (source_entity_id, category, target_entity_id) "
        "VALUES ('trip-france', 'contacts', 'contact-mike')",
    )

    rename = {"contact-blair-nicol": "contact-03f3902d",
              "contact-mike": "contact-7c9f0fd7"}
    count = await rewrite_entity_relations(db, rename)

    assert count == 2
    rels = await db.fetch_all(
        "SELECT target_entity_id FROM memory_entity_relations WHERE source_entity_id = 'trip-france'",
    )
    targets = {r["target_entity_id"] for r in rels}
    assert targets == {"contact-03f3902d", "contact-7c9f0fd7"}


async def test_run_cleanup_end_to_end(db):
    from cyborg_server.services.memory.cleanup import run_cleanup

    # Three entities: canonical Blair, name-slug Blair, orphan Bob Sr x2
    await _insert_contact_entity(db, "contact-03f3902d", "Blair Nicol")
    await _insert_contact_entity(db, "contact-blair-nicol", "Blair Nicol")
    await _insert_contact_entity(db, "bob-sr", "Bob Sr.")
    await _insert_contact_entity(db, "contact-bob-sr", "Bob Sr.")
    # A claim and bulletin referencing the doomed IDs
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

    # Canonical Blair still exists, name-slug deleted
    canon = await db.fetch_one("SELECT * FROM memory_entities WHERE entity_id = 'contact-03f3902d'")
    assert canon is not None
    dup = await db.fetch_one("SELECT * FROM memory_entities WHERE entity_id = 'contact-blair-nicol'")
    assert dup is None
    # Bob Sr winner kept, loser deleted
    winner = await db.fetch_one("SELECT * FROM memory_entities WHERE entity_id = 'contact-bob-sr'")
    assert winner is not None
    loser = await db.fetch_one("SELECT * FROM memory_entities WHERE entity_id = 'bob-sr'")
    assert loser is None
    # Claim rewritten
    c1 = await db.fetch_one("SELECT subject_id, object_id FROM memory_claims WHERE id = 'claim-001'")
    assert c1["subject_id"] == "contact-03f3902d"
    assert c1["object_id"] == "contact-bob-sr"
    # Bulletin entity ref rewritten
    brefs = await db.fetch_all(
        "SELECT entity_id FROM memory_entity_bulletins WHERE bulletin_id = 'b-1'",
    )
    assert brefs[0]["entity_id"] == "contact-03f3902d"
    # Canon enriched with FK
    fm = json.loads(canon["extra_frontmatter"])
    assert fm["contact_id"] == "03f3902d-330b-4f15-bf2a-b1385a917677"
    assert fm["phone_number"] == "+61401589328"

    assert summary["merged"] >= 2
    assert summary["rewritten_claims"] >= 1
    assert summary["rewritten_bulletins"] >= 1
