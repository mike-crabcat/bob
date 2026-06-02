"""Tests for memory cleanup pipeline (merge_entity_docs, build_renaming_map, rewrites, run_cleanup)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cyborg_server.services.memory.models import EntityDocument, serialize_frontmatter


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
