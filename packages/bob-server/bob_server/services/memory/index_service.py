"""Index service — build and maintain derived lookup structures.

With SQLite storage, indexes are maintained as tables (memory_aliases,
memory_entity_relations) rather than YAML files.  The file-based functions
are kept for backward compatibility but are no-ops.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


async def rebuild_all(db: Any) -> dict[str, int]:
    """Rebuild the memory_aliases table from memory_entities."""
    # Clear existing aliases
    await db.execute("DELETE FROM memory_aliases")

    rows = await db.fetch_all(
        "SELECT entity_id, display_name FROM memory_entities WHERE display_name != ''"
    )
    count = 0
    for r in rows:
        eid = r["entity_id"]
        name = r["display_name"]
        await db.execute(
            "INSERT OR IGNORE INTO memory_aliases (alias, entity_id) VALUES (?, ?)",
            (name, eid),
        )
        await db.execute(
            "INSERT OR IGNORE INTO memory_aliases (alias, entity_id) VALUES (?, ?)",
            (name.lower(), eid),
        )
        count += 1

    entity_count_row = await db.fetch_one("SELECT COUNT(*) AS c FROM memory_entities")
    entity_count = entity_count_row["c"] if entity_count_row else 0

    logger.info("Rebuilt aliases: %d entities, %d names", entity_count, count)
    return {"entities": entity_count, "aliases": count}


# ── Legacy file-based functions (no-ops) ────────────────────────


def build_entity_map(memory_dir: Path) -> dict[str, dict[str, str]]:
    """Legacy: use SQL queries instead."""
    return {}


def build_reverse_links(memory_dir: Path) -> dict[str, list[str]]:
    """Legacy: use SQL queries instead."""
    return {}


def build_aliases(memory_dir: Path) -> dict[str, str]:
    """Legacy: use SQL queries instead."""
    return {}


def build_memory_index_text(memory_dir: Path) -> str:
    """Legacy: use build_memory_index_text_db instead."""
    return ""
