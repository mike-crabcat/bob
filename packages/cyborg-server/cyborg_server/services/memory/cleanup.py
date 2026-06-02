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


def build_renaming_map(
    memory_dir: Path,
    directory: ContactDirectory | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Scan contact entities and compute (rename, merge_into) maps.

    - rename: {old_id: new_id} — every reference to old_id should be rewritten to new_id
    - merge_into: {dup_id: canonical_id} — dup_id's body should be merged into
      canonical_id before deletion

    For non-canonical IDs that match a DB contact by display_name, the new_id
    is the canonical contact-{hex8}. For orphan duplicates that share a
    display_name with no DB match, the lexicographically smallest `contact-`-
    prefixed ID wins (or the smallest overall if neither has the prefix).
    """
    contact_dir = memory_dir / "entities" / "contact"
    if not contact_dir.is_dir():
        return {}, {}

    rows: list[tuple[str, str]] = []
    for md_file in sorted(contact_dir.glob("*.md")):
        fm, _ = parse_frontmatter(md_file.read_text(encoding="utf-8"))
        rows.append((md_file.stem, fm.get("display_name", "")))

    rename: dict[str, str] = {}

    # Step 1: non-canonical -> canonical via DB lookup
    for entity_id, name in rows:
        if is_canonical_contact_id(entity_id):
            continue
        if directory is None or not name:
            continue
        record = directory.get_by_name(name)
        if record is not None:
            rename[entity_id] = record.canonical_id

    # Step 2: orphan duplicates (same display_name) — pick a winner
    by_name: dict[str, list[str]] = {}
    for entity_id, name in rows:
        if not name:
            continue
        if entity_id in rename:
            continue
        by_name.setdefault(name, []).append(entity_id)

    for name, ids in by_name.items():
        if len(ids) < 2:
            continue

        def sort_key(eid: str) -> tuple[int, str]:
            return (0 if eid.startswith("contact-") else 1, eid)

        ids_sorted = sorted(ids, key=sort_key)
        winner = ids_sorted[0]
        for loser in ids_sorted[1:]:
            rename[loser] = winner

    # Step 3: merge_into — only the entries where the destination currently
    # exists on disk (so we need to merge bodies before deleting).
    existing_ids = {eid for eid, _ in rows}
    merge_into: dict[str, str] = {}
    for old, new in rename.items():
        if new in existing_ids:
            merge_into[old] = new

    return rename, merge_into


def _read_entity_doc(path: Path) -> EntityDocument | None:
    if not path.is_file():
        return None
    fm, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    return EntityDocument(
        entity_id=fm.get("entity_id", path.stem),
        entity_type=fm.get("entity_type", ""),
        display_name=fm.get("display_name", ""),
        status=fm.get("status", "active"),
        extra_frontmatter={
            k: v for k, v in fm.items()
            if k not in {"entity_id", "entity_type", "display_name", "status"}
        },
        body=body,
    )


def _rewrite_refs(value: str, rename: dict[str, str]) -> str:
    return rename.get(value, value)


def rewrite_claims(memory_dir: Path, rename: dict[str, str]) -> int:
    """Rewrite subject_id/object_id in every claim file. Returns changed count."""
    claims_dir = memory_dir / "claims"
    if not claims_dir.is_dir():
        return 0
    changed = 0
    for md_file in claims_dir.glob("*.md"):
        raw = md_file.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(raw)
        old_subj = fm.get("subject_id", "")
        old_obj = fm.get("object_id")
        new_subj = _rewrite_refs(old_subj, rename)
        new_obj = _rewrite_refs(old_obj, rename) if isinstance(old_obj, str) else old_obj
        if new_subj != old_subj or new_obj != old_obj:
            fm["subject_id"] = new_subj
            fm["object_id"] = new_obj
            md_file.write_text(serialize_frontmatter(fm, body), encoding="utf-8")
            changed += 1
    return changed


def rewrite_bulletin_entities(memory_dir: Path, rename: dict[str, str]) -> int:
    """Rewrite entities.contacts[].id in every bulletin. Dedupe within each bulletin."""
    bulletins_dir = memory_dir / "bulletins"
    if not bulletins_dir.is_dir():
        return 0
    changed = 0
    for md_file in bulletins_dir.rglob("*.md"):
        raw = md_file.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(raw)
        entities = fm.get("entities") or {}
        contacts = entities.get("contacts") or []
        if not isinstance(contacts, list):
            continue
        new_contacts: list[dict] = []
        seen: set[str] = set()
        local_changed = False
        for entry in contacts:
            if isinstance(entry, str):
                entry = {"id": entry}
            old_id = entry.get("id", "")
            new_id = _rewrite_refs(old_id, rename)
            if new_id != old_id:
                local_changed = True
            if new_id in seen:
                local_changed = True
                continue
            seen.add(new_id)
            new_entry = dict(entry)
            new_entry["id"] = new_id
            new_contacts.append(new_entry)
        if local_changed:
            entities["contacts"] = new_contacts
            fm["entities"] = entities
            md_file.write_text(serialize_frontmatter(fm, body), encoding="utf-8")
            changed += 1
    return changed


def rewrite_entity_related(memory_dir: Path, rename: dict[str, str]) -> int:
    """Rewrite Related Entities contact refs in every entity document."""
    entities_dir = memory_dir / "entities"
    if not entities_dir.is_dir():
        return 0
    changed = 0
    for type_dir in entities_dir.iterdir():
        if not type_dir.is_dir():
            continue
        for md_file in type_dir.glob("*.md"):
            raw = md_file.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(raw)
            new_body = body
            for old, new in rename.items():
                # only match as a bullet item to avoid partial replacements
                new_body = re.sub(
                    rf"(\s*-\s+){re.escape(old)}(\s*)$",
                    rf"\g<1>{new}\g<2>",
                    new_body,
                    flags=re.MULTILINE,
                )
            if new_body != body:
                md_file.write_text(serialize_frontmatter(fm, new_body), encoding="utf-8")
                changed += 1
    return changed


async def run_cleanup(
    memory_dir: Path,
    directory: ContactDirectory | None,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """End-to-end cleanup of contact entity duplicates.

    Steps:
      1. Build renaming map
      2. Merge duplicate entity bodies into canonical entity files
      3. Delete the duplicate files
      4. Rewrite every claim, bulletin, and entity Related Entities section
      5. Enrich canonical contact entity frontmatter with contact_id/email/phone
    """
    rename, merge_into = build_renaming_map(memory_dir, directory)

    merged = 0
    deleted = 0
    contact_dir = memory_dir / "entities" / "contact"

    # Step 2 + 3: merge bodies, then delete
    for dup_id, canon_id in merge_into.items():
        if dry_run:
            continue
        dup_path = contact_dir / f"{dup_id}.md"
        canon_path = contact_dir / f"{canon_id}.md"
        if not dup_path.is_file() or not canon_path.is_file():
            continue
        dup_doc = _read_entity_doc(dup_path)
        canon_doc = _read_entity_doc(canon_path)
        if dup_doc and canon_doc:
            merged_doc = merge_entity_docs(canon_doc, dup_doc)
            canon_path.write_text(
                serialize_frontmatter(
                    {
                        "entity_id": merged_doc.entity_id,
                        "entity_type": merged_doc.entity_type,
                        "display_name": merged_doc.display_name,
                        "status": merged_doc.status,
                        **merged_doc.extra_frontmatter,
                    },
                    merged_doc.body,
                ),
                encoding="utf-8",
            )
            merged += 1
        dup_path.unlink()
        deleted += 1

    # Pure renames (no on-disk canonical target) — just rename the file
    for old, new in rename.items():
        if old in merge_into:
            continue
        if dry_run:
            continue
        old_path = contact_dir / f"{old}.md"
        new_path = contact_dir / f"{new}.md"
        if old_path.is_file() and not new_path.is_file():
            raw = old_path.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(raw)
            fm["entity_id"] = new
            new_path.write_text(serialize_frontmatter(fm, body), encoding="utf-8")
            old_path.unlink()
            deleted += 1

    # Step 4: rewrite refs
    rewritten_claims = rewrite_claims(memory_dir, rename) if not dry_run else 0
    rewritten_bulletins = rewrite_bulletin_entities(memory_dir, rename) if not dry_run else 0
    rewritten_related = rewrite_entity_related(memory_dir, rename) if not dry_run else 0

    # Step 5: enrich canonical entities with FK
    enriched = 0
    if directory is not None and not dry_run:
        for md_file in contact_dir.glob("*.md"):
            record = directory.get_by_canonical_id(md_file.stem)
            if record is None:
                continue
            raw = md_file.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(raw)
            fm["contact_id"] = record.uuid
            if record.email:
                fm["email"] = record.email
            if record.phone_number:
                fm["phone_number"] = record.phone_number
            md_file.write_text(serialize_frontmatter(fm, body), encoding="utf-8")
            enriched += 1

    return {
        "renamed": len(rename),
        "merged": merged,
        "deleted": deleted,
        "rewritten_claims": rewritten_claims,
        "rewritten_bulletins": rewritten_bulletins,
        "rewritten_related": rewritten_related,
        "enriched": enriched,
    }
