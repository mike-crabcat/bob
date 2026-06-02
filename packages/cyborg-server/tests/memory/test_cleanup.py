"""Tests for memory cleanup pipeline (merge_entity_docs, build_renaming_map, rewrites, run_cleanup)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cyborg_server.services.memory.contact_directory import ContactDirectory, ContactRecord
from cyborg_server.services.memory.models import EntityDocument, parse_frontmatter, serialize_frontmatter


def _write_contact_entity(memory_dir: Path, entity_id: str, display_name: str) -> None:
    p = memory_dir / "entities" / "contact" / f"{entity_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\n"
        f"entity_id: {entity_id}\n"
        "entity_type: contact\n"
        f"display_name: {display_name}\n"
        "status: active\n"
        "---\n\n"
        f"# {display_name}\n",
        encoding="utf-8",
    )


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


def test_renaming_map_matches_name_slug_to_canonical(tmp_path):
    from cyborg_server.services.memory.cleanup import build_renaming_map

    memory_dir = tmp_path / "memory"
    _write_contact_entity(memory_dir, "contact-03f3902d", "Blair Nicol")
    _write_contact_entity(memory_dir, "contact-blair-nicol", "Blair Nicol")
    _write_contact_entity(memory_dir, "unresolved-contact-gareth", "Gareth")

    directory = ContactDirectory([
        ContactRecord(
            uuid="03f3902d-330b-4f15-bf2a-b1385a917677",
            canonical_id="contact-03f3902d",
            name="Blair Nicol",
            phone_number="+61401589328",
            email="",
        ),
    ])

    rename, merge_into = build_renaming_map(memory_dir, directory)

    assert rename.get("contact-blair-nicol") == "contact-03f3902d"
    assert merge_into.get("contact-blair-nicol") == "contact-03f3902d"
    assert "unresolved-contact-gareth" not in rename


def test_renaming_map_handles_two_orphans_with_same_name(tmp_path):
    from cyborg_server.services.memory.cleanup import build_renaming_map

    memory_dir = tmp_path / "memory"
    _write_contact_entity(memory_dir, "bob-sr", "Bob Sr.")
    _write_contact_entity(memory_dir, "contact-bob-sr", "Bob Sr.")

    rename, merge_into = build_renaming_map(memory_dir, directory=None)

    assert rename.get("bob-sr") == "contact-bob-sr"
    assert merge_into.get("bob-sr") == "contact-bob-sr"


def _write_claim(memory_dir: Path, claim_id: str, subject_id: str, object_id: str | None) -> None:
    p = memory_dir / "claims" / f"{claim_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    fm = {"id": claim_id, "type": "fact", "subject_id": subject_id,
          "predicate": "x", "object_id": object_id, "status": "active",
          "source_bulletins": [], "visibility": "private", "scope": []}
    p.write_text(serialize_frontmatter(fm, "# Claim\n\nbody"), encoding="utf-8")


def test_rewrite_claims_renames_subject_and_object_ids(tmp_path):
    from cyborg_server.services.memory.cleanup import rewrite_claims

    memory_dir = tmp_path / "memory"
    _write_claim(memory_dir, "claim-001", "contact-blair-nicol", None)
    _write_claim(memory_dir, "claim-002", "contact-mike", "contact-blair-nicol")
    _write_claim(memory_dir, "claim-003", "trip-bali", "location-x")

    rename = {"contact-blair-nicol": "contact-03f3902d",
              "contact-mike": "contact-7c9f0fd7"}
    count = rewrite_claims(memory_dir, rename)

    assert count == 2
    c1 = parse_frontmatter((memory_dir / "claims" / "claim-001.md").read_text())[0]
    assert c1["subject_id"] == "contact-03f3902d"
    c2 = parse_frontmatter((memory_dir / "claims" / "claim-002.md").read_text())[0]
    assert c2["subject_id"] == "contact-7c9f0fd7"
    assert c2["object_id"] == "contact-03f3902d"
    c3 = parse_frontmatter((memory_dir / "claims" / "claim-003.md").read_text())[0]
    assert c3["subject_id"] == "trip-bali"


def _write_bulletin(memory_dir: Path, bid: str, contact_refs: list) -> None:
    p = memory_dir / "bulletins" / "2026" / "06" / f"{bid}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    fm = {"id": bid, "entities": {"contacts": contact_refs,
            "groups": [], "channels": [], "trips": [], "locations": [],
            "events": [], "tasks": [], "artifacts": [], "decisions": []}}
    p.write_text(serialize_frontmatter(fm, "# Update\n\nbody"), encoding="utf-8")


def test_rewrite_bulletin_entities_renames_contact_refs(tmp_path):
    from cyborg_server.services.memory.cleanup import rewrite_bulletin_entities

    memory_dir = tmp_path / "memory"
    _write_bulletin(memory_dir, "b-1", [
        {"id": "contact-blair-nicol", "matched_from": "Blair"},
        {"id": "contact-03f3902d"},
    ])
    _write_bulletin(memory_dir, "b-2", [{"id": "contact-mike"}])

    rename = {"contact-blair-nicol": "contact-03f3902d",
              "contact-mike": "contact-7c9f0fd7"}
    count = rewrite_bulletin_entities(memory_dir, rename)

    assert count == 2
    b1 = parse_frontmatter((memory_dir / "bulletins" / "2026" / "06" / "b-1.md").read_text())[0]
    contact_ids = [c["id"] for c in b1["entities"]["contacts"]]
    assert contact_ids.count("contact-03f3902d") == 1
    b2 = parse_frontmatter((memory_dir / "bulletins" / "2026" / "06" / "b-2.md").read_text())[0]
    assert b2["entities"]["contacts"][0]["id"] == "contact-7c9f0fd7"


def test_rewrite_entity_related_renames_contact_refs(tmp_path):
    from cyborg_server.services.memory.cleanup import rewrite_entity_related

    memory_dir = tmp_path / "memory"
    contact_dir = memory_dir / "entities" / "contact"
    contact_dir.mkdir(parents=True)
    trip_dir = memory_dir / "entities" / "trip"
    trip_dir.mkdir(parents=True)

    # A trip doc referencing the doomed contact-blair-nicol
    trip_path = trip_dir / "trip-france.md"
    trip_path.write_text(
        "---\nentity_id: trip-france\nentity_type: trip\ndisplay_name: France\nstatus: active\n---\n\n"
        "## Related Entities\n\n"
        "contacts:\n  - contact-blair-nicol\n  - contact-mike\n"
        "groups: []\nchannels: []\ntrips: []\nlocations: []\n"
        "events: []\ntasks: []\nartifacts: []\ndecisions: []\n",
        encoding="utf-8",
    )

    rename = {"contact-blair-nicol": "contact-03f3902d",
              "contact-mike": "contact-7c9f0fd7"}
    count = rewrite_entity_related(memory_dir, rename)

    assert count == 1
    body = trip_path.read_text(encoding="utf-8")
    assert "contact-blair-nicol" not in body
    assert "contact-03f3902d" in body
    assert "contact-7c9f0fd7" in body


@pytest.mark.asyncio
async def test_run_cleanup_end_to_end(tmp_path):
    from cyborg_server.services.memory.cleanup import run_cleanup

    memory_dir = tmp_path / "memory"
    # Three entities: canonical Blair, name-slug Blair, orphan Bob Sr x2
    _write_contact_entity(memory_dir, "contact-03f3902d", "Blair Nicol")
    _write_contact_entity(memory_dir, "contact-blair-nicol", "Blair Nicol")
    _write_contact_entity(memory_dir, "bob-sr", "Bob Sr.")
    _write_contact_entity(memory_dir, "contact-bob-sr", "Bob Sr.")
    # A claim and bulletin referencing the doomed IDs
    _write_claim(memory_dir, "claim-001", "contact-blair-nicol", "bob-sr")
    _write_bulletin(memory_dir, "b-1", [{"id": "contact-blair-nicol"}])

    directory = ContactDirectory([
        ContactRecord("03f3902d-330b-4f15-bf2a-b1385a917677", "contact-03f3902d",
                      "Blair Nicol", "+61401589328", ""),
    ])

    summary = await run_cleanup(memory_dir, directory, dry_run=False)

    assert (memory_dir / "entities" / "contact" / "contact-03f3902d.md").is_file()
    assert not (memory_dir / "entities" / "contact" / "contact-blair-nicol.md").is_file()
    assert (memory_dir / "entities" / "contact" / "contact-bob-sr.md").is_file()
    assert not (memory_dir / "entities" / "contact" / "bob-sr.md").is_file()
    c1 = parse_frontmatter((memory_dir / "claims" / "claim-001.md").read_text())[0]
    assert c1["subject_id"] == "contact-03f3902d"
    assert c1["object_id"] == "contact-bob-sr"
    b1 = parse_frontmatter((memory_dir / "bulletins" / "2026" / "06" / "b-1.md").read_text())[0]
    assert b1["entities"]["contacts"][0]["id"] == "contact-03f3902d"
    canon = parse_frontmatter(
        (memory_dir / "entities" / "contact" / "contact-03f3902d.md").read_text()
    )[0]
    assert canon["contact_id"] == "03f3902d-330b-4f15-bf2a-b1385a917677"
    assert canon["phone_number"] == "+61401589328"

    assert summary["merged"] >= 2
    assert summary["rewritten_claims"] >= 1
    assert summary["rewritten_bulletins"] >= 1
