"""Cleanup of duplicate person entity records.

Merges duplicate person entities (e.g. person-blair-nicol, contact-blair-nicol)
into canonical person-{slug} rows and rewrites every reference (claims, bulletin
entity refs, entity relations) to point at the canonical ID.

In v7, entities have no body — cleanup rewrites claim references and deletes
duplicate entity records. Claims are the source of truth.
"""

from __future__ import annotations

from typing import Any

from bob_server.services.memory.contact_directory import ContactDirectory


async def build_renaming_map(
    db: Any,
    directory: ContactDirectory | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Scan person entities and compute (rename, merge_into) maps."""
    rows = await db.fetch_all(
        "SELECT entity_id, display_name FROM memory_entities WHERE entity_type = 'person'"
    )

    rename: dict[str, str] = {}

    # Step 1: non-canonical -> canonical via DB lookup
    for r in rows:
        entity_id = r["entity_id"]
        name = r["display_name"] or ""
        # Skip canonical person-{slug} IDs — these are the target format
        if entity_id.startswith("person-") and not entity_id.startswith("person-new-"):
            continue
        if directory is None or not name:
            continue
        record = directory.get_by_name(name)
        if record is not None:
            # Map to person-{slug} format
            import re
            slug = re.sub(r"[^a-z0-9\-]", "", name.strip().lower().replace(" ", "-"))
            rename[entity_id] = f"person-{slug}"

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
            return (0 if eid.startswith("person-") else 1, eid)
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
        result = await db.execute(
            "UPDATE memory_claims SET subject_id = ? WHERE subject_id = ?",
            (new, old),
        )
        changed += result
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
        rows = await db.fetch_all(
            "SELECT bulletin_id FROM memory_entity_bulletins WHERE entity_id = ?",
            (old,),
        )
        if not rows:
            continue

        for r in rows:
            bid = r["bulletin_id"]
            existing = await db.fetch_one(
                "SELECT 1 FROM memory_entity_bulletins WHERE entity_id = ? AND bulletin_id = ? LIMIT 1",
                (new, bid),
            )
            if existing:
                await db.execute(
                    "DELETE FROM memory_entity_bulletins WHERE entity_id = ? AND bulletin_id = ?",
                    (old, bid),
                )
            else:
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
        result = await db.execute(
            "UPDATE memory_entity_relations SET target_entity_id = ? WHERE target_entity_id = ?",
            (new, old),
        )
        changed += result
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

    deleted = 0

    # Step 1: delete duplicate entities (claims already point to canonical via rewrite)
    for dup_id, canon_id in merge_into.items():
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

    return {
        "renamed": len(rename),
        "merged": 0,
        "deleted": deleted,
        "rewritten_claims": rewritten_claims,
        "rewritten_bulletins": rewritten_bulletins,
        "rewritten_related": rewritten_related,
    }
