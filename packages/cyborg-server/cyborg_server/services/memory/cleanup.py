"""Cleanup of duplicate contact entity documents.

Merges duplicate contact entities (e.g. contact-blair-nicol, unresolved-contact-blair)
into canonical contact-{hex8} rows and rewrites every reference (claims, bulletin
entity refs, entity relations) to point at the canonical ID.
"""

from __future__ import annotations

import json
import re
from typing import Any

from cyborg_server.services.memory.contact_directory import ContactDirectory
from cyborg_server.services.memory.models import EntityDocument
from cyborg_server.services.memory.reconcile import is_canonical_contact_id


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
    """Merge *duplicate* into *canonical*, returning the merged document."""
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


async def build_renaming_map(
    db: Any,
    directory: ContactDirectory | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Scan contact entities and compute (rename, merge_into) maps."""
    rows = await db.fetch_all(
        "SELECT entity_id, display_name FROM memory_entities WHERE entity_type = 'contact'"
    )

    rename: dict[str, str] = {}

    # Step 1: non-canonical -> canonical via DB lookup
    for r in rows:
        entity_id = r["entity_id"]
        name = r["display_name"] or ""
        if is_canonical_contact_id(entity_id):
            continue
        if directory is None or not name:
            continue
        record = directory.get_by_name(name)
        if record is not None:
            rename[entity_id] = record.canonical_id

    # Step 2: orphan duplicates (same display_name) — pick a winner
    by_name: dict[str, list[str]] = {}
    for r in rows:
        entity_id = r["entity_id"]
        name = r["display_name"] or ""
        if not name or entity_id in rename:
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

    # Step 3: merge_into — only where destination exists
    existing_ids = {r["entity_id"] for r in rows}
    merge_into: dict[str, str] = {}
    for old, new in rename.items():
        if new in existing_ids:
            merge_into[old] = new

    return rename, merge_into


async def rewrite_claims(db: Any, rename: dict[str, str]) -> int:
    """Rewrite subject_id/object_id in claims. Returns changed count."""
    changed = 0
    for old, new in rename.items():
        # subject_id
        result = await db.execute(
            "UPDATE memory_claims SET subject_id = ? WHERE subject_id = ?",
            (new, old),
        )
        changed += result
        # object_id
        result = await db.execute(
            "UPDATE memory_claims SET object_id = ? WHERE object_id = ?",
            (new, old),
        )
        changed += result
    return changed


async def rewrite_bulletin_entities(db: Any, rename: dict[str, str]) -> int:
    """Rewrite entity_id in entity↔bulletin join rows. Dedupe within each bulletin."""
    changed = 0
    for old, new in rename.items():
        # Find bulletins linked to the old entity_id
        rows = await db.fetch_all(
            "SELECT bulletin_id FROM memory_entity_bulletins WHERE entity_id = ?",
            (old,),
        )
        if not rows:
            continue

        for r in rows:
            bid = r["bulletin_id"]
            # Check if new already linked to this bulletin
            existing = await db.fetch_one(
                "SELECT 1 FROM memory_entity_bulletins WHERE entity_id = ? AND bulletin_id = ? LIMIT 1",
                (new, bid),
            )
            if existing:
                # Delete the old one (new already exists)
                await db.execute(
                    "DELETE FROM memory_entity_bulletins WHERE entity_id = ? AND bulletin_id = ?",
                    (old, bid),
                )
            else:
                # Rename
                await db.execute(
                    "UPDATE memory_entity_bulletins SET entity_id = ? WHERE entity_id = ? AND bulletin_id = ?",
                    (new, old, bid),
                )
            changed += 1
    return changed


async def rewrite_entity_relations(db: Any, rename: dict[str, str]) -> int:
    """Rewrite target_entity_id and source_entity_id in entity relations."""
    changed = 0
    for old, new in rename.items():
        # target_entity_id
        result = await db.execute(
            "UPDATE memory_entity_relations SET target_entity_id = ? WHERE target_entity_id = ?",
            (new, old),
        )
        changed += result
        # source_entity_id
        result = await db.execute(
            "UPDATE memory_entity_relations SET source_entity_id = ? WHERE source_entity_id = ?",
            (new, old),
        )
        changed += result
    return changed


async def run_cleanup(
    db: Any,
    directory: ContactDirectory | None,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """End-to-end cleanup of contact entity duplicates."""
    rename, merge_into = await build_renaming_map(db, directory)

    if dry_run:
        return {"renamed": len(rename), "merged": 0, "deleted": 0, "dry_run": True}

    merged = 0
    deleted = 0

    # Step 1: merge duplicate bodies into canonical, then delete
    for dup_id, canon_id in merge_into.items():
        dup_row = await db.fetch_one(
            "SELECT * FROM memory_entities WHERE entity_id = ?", (dup_id,)
        )
        canon_row = await db.fetch_one(
            "SELECT * FROM memory_entities WHERE entity_id = ?", (canon_id,)
        )
        if not dup_row or not canon_row:
            continue

        dup_doc = EntityDocument(
            entity_id=dup_row["entity_id"],
            entity_type=dup_row["entity_type"],
            display_name=dup_row["display_name"] or "",
            status=dup_row["status"] or "active",
            extra_frontmatter=json.loads(dup_row["extra_frontmatter"]) if dup_row["extra_frontmatter"] else {},
            body=dup_row["body"] or "",
        )
        canon_doc = EntityDocument(
            entity_id=canon_row["entity_id"],
            entity_type=canon_row["entity_type"],
            display_name=canon_row["display_name"] or "",
            status=canon_row["status"] or "active",
            extra_frontmatter=json.loads(canon_row["extra_frontmatter"]) if canon_row["extra_frontmatter"] else {},
            body=canon_row["body"] or "",
        )
        merged_doc = merge_entity_docs(canon_doc, dup_doc)
        await db.execute(
            "UPDATE memory_entities SET body = ?, display_name = ?, extra_frontmatter = ? "
            "WHERE entity_id = ?",
            (
                merged_doc.body,
                merged_doc.display_name,
                json.dumps(merged_doc.extra_frontmatter),
                canon_id,
            ),
        )
        merged += 1

        # Delete duplicate
        await db.execute("DELETE FROM memory_entities WHERE entity_id = ?", (dup_id,))
        deleted += 1

    # Pure renames (no merge target) — just update entity_id
    for old, new in rename.items():
        if old in merge_into:
            continue
        exists = await db.fetch_one(
            "SELECT 1 FROM memory_entities WHERE entity_id = ?", (old,)
        )
        if exists:
            await db.execute(
                "UPDATE memory_entities SET entity_id = ? WHERE entity_id = ?",
                (new, old),
            )
            deleted += 1

    # Step 2: rewrite refs
    rewritten_claims = await rewrite_claims(db, rename)
    rewritten_bulletins = await rewrite_bulletin_entities(db, rename)
    rewritten_related = await rewrite_entity_relations(db, rename)

    # Step 3: enrich canonical entities with FK
    enriched = 0
    if directory is not None:
        rows = await db.fetch_all(
            "SELECT entity_id FROM memory_entities WHERE entity_type = 'contact'"
        )
        for r in rows:
            record = directory.get_by_canonical_id(r["entity_id"])
            if record is None:
                continue
            row = await db.fetch_one(
                "SELECT extra_frontmatter FROM memory_entities WHERE entity_id = ?",
                (r["entity_id"],),
            )
            if not row:
                continue
            fm = json.loads(row["extra_frontmatter"]) if row["extra_frontmatter"] else {}
            fm["contact_id"] = record.uuid
            if record.email:
                fm["email"] = record.email
            if record.phone_number:
                fm["phone_number"] = record.phone_number
            await db.execute(
                "UPDATE memory_entities SET extra_frontmatter = ? WHERE entity_id = ?",
                (json.dumps(fm), r["entity_id"]),
            )
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
