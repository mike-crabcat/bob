"""One-shot cleanup of duplicate contact entity documents.

The bulletin → claim → entity pipeline previously produced duplicate
contact entities because:

  1. The bulletin generator was free to invent ``contact-{name-slug}`` and
     ``unresolved-contact-{name}`` IDs even for contacts that exist in the
     cyborg contacts DB.
  2. The entity-update step trusted the LLM's chosen ID and wrote a new
     file under that ID, side-by-side with the canonical file.

This module merges those duplicates back into canonical
``contact-{hex8}`` files and rewrites every reference (claims, bulletin
entity refs, entity Related Entities sections) to point at the canonical
ID. Canonical contact entities also get ``contact_id``/``email``/
``phone_number`` frontmatter fields as a real foreign key back to the DB.
"""

from __future__ import annotations

import re
from pathlib import Path

from cyborg_server.services.memory.contact_directory import ContactDirectory
from cyborg_server.services.memory.models import (
    ENTITY_CATEGORIES,
    EntityDocument,
    parse_frontmatter,
    serialize_frontmatter,
)
from cyborg_server.services.memory.reconcile import is_canonical_contact_id


_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _extract_section(body: str, heading: str) -> str:
    """Return the contents under `## heading`, or empty string if missing."""
    pat = re.compile(
        rf"^##\s+{re.escape(heading)}\s*\n(.*?)(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pat.search(body)
    return m.group(1).strip() if m else ""


def _parse_related_entities(section_body: str) -> dict[str, list[str]]:
    """Parse a Related Entities section into {category_singular: [ids]}."""
    out: dict[str, list[str]] = {}
    current = None
    for line in section_body.splitlines():
        line = line.rstrip()
        if not line:
            continue
        # "key:" or "key: []" — start a new category
        m = re.match(r"^(\w+):\s*(\[\])?\s*$", line)
        if m:
            current = m.group(1)
            out.setdefault(current, [])
            continue
        if current and line.lstrip().startswith("-"):
            item = line.lstrip("- ").strip()
            if item and item != "[]":
                out.setdefault(current, []).append(item)
    return out


def _serialize_related_entities(related: dict[str, list[str]]) -> str:
    cats = ["contacts", "groups", "channels", "trips", "locations",
            "events", "tasks", "artifacts", "decisions"]
    lines = ["## Related Entities", ""]
    for cat in cats:
        items = sorted(set(related.get(cat, [])))
        if items:
            lines.append(f"{cat}:")
            for item in items:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{cat}: []")
    return "\n".join(lines) + "\n"


def merge_entity_docs(canonical: EntityDocument, duplicate: EntityDocument) -> EntityDocument:
    """Merge *duplicate* into *canonical*, returning the merged document.

    Sections combined: Summary, Current State, Timeline, Source Bulletins,
    Related Entities. The canonical entity_id and display_name win on conflict.
    """
    def combine_text(a: str, b: str) -> str:
        a_lines = [ln.strip() for ln in a.splitlines() if ln.strip()]
        b_lines = [ln.strip() for ln in b.splitlines() if ln.strip()]
        seen: set[str] = set()
        out: list[str] = []
        for ln in a_lines + b_lines:
            if ln not in seen:
                seen.add(ln)
                out.append(ln)
        return "\n".join(out)

    sum_a = _extract_section(canonical.body, "Summary")
    sum_b = _extract_section(duplicate.body, "Summary")
    state_a = _extract_section(canonical.body, "Current State")
    state_b = _extract_section(duplicate.body, "Current State")
    timeline_a = _extract_section(canonical.body, "Timeline")
    timeline_b = _extract_section(duplicate.body, "Timeline")
    sources_a = _extract_section(canonical.body, "Source Bulletins")
    sources_b = _extract_section(duplicate.body, "Source Bulletins")

    rel_a = _parse_related_entities(_extract_section(canonical.body, "Related Entities"))
    rel_b = _parse_related_entities(_extract_section(duplicate.body, "Related Entities"))
    merged_rel: dict[str, list[str]] = {}
    for key in set(rel_a.keys()) | set(rel_b.keys()):
        merged_rel[key] = rel_a.get(key, []) + rel_b.get(key, [])

    sections = ["## Summary", "", combine_text(sum_a, sum_b), ""]
    if state_a or state_b:
        sections += ["## Current State", "", combine_text(state_a, state_b), ""]
    sections += [
        _serialize_related_entities(merged_rel),
        "",
        "## Timeline", "", combine_text(timeline_a, timeline_b), "",
        "## Source Bulletins", "", combine_text(sources_a, sources_b),
    ]

    return EntityDocument(
        entity_id=canonical.entity_id,
        entity_type=canonical.entity_type,
        display_name=canonical.display_name or duplicate.display_name,
        status=canonical.status,
        extra_frontmatter={**duplicate.extra_frontmatter, **canonical.extra_frontmatter},
        body="\n".join(sections) + "\n",
    )
