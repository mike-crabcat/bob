"""Tests for memory cleanup pipeline (merge_entity_docs, build_renaming_map, rewrites, run_cleanup)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cyborg_server.services.memory.contact_directory import ContactDirectory, ContactRecord
from cyborg_server.services.memory.models import EntityDocument, serialize_frontmatter


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
