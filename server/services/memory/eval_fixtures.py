"""Eval-fixture seeding for the memory tables (Phase 1, 2026-09-28).

The memory tables' SQL lives here, not in the eval case files —
test_sql_ownership enforces the boundary, and seeding through this
module also populates the FTS index the recall path resolves through
(raw-SQL inserts in the case file bypassed it: the 2026-09-28 baseline
M3 case queried honestly and still couldn't find the seeded person).

All fixture IDs are prefixed ``eval-``; ``cleanup_eval_fixtures``
removes exactly those rows and index entries.
"""

from __future__ import annotations

import uuid
from typing import Any


async def seed_entity(db: Any, entity_id: str, entity_type: str,
                      display_name: str,
                      claims: list[tuple[str, str]]) -> None:
    """Create (or reactivate) an eval entity with scalar claims, indexed."""
    await db.execute(
        "INSERT INTO memory_entities (entity_id, entity_type, display_name, "
        "status) VALUES (?, ?, ?, 'active') "
        "ON CONFLICT(entity_id) DO UPDATE SET status='active'",
        (entity_id, entity_type, display_name))
    # FTS row (entity_id, display_name, rendered_body) — recall resolves
    # through this index; claims render into the body so name- and
    # content-shaped queries both land.
    body = " ".join(f"{k}: {v}" for k, v in claims)
    await db.execute(
        "INSERT INTO memory_entities_fts (entity_id, display_name, "
        "rendered_body) VALUES (?, ?, ?)",
        (entity_id, display_name, body))
    # Embedding, the way production claim-writes populate it (claim_service
    # embeds the rendered body): without this, fresh seeds are invisible to
    # the embedding search path and evals under-measure recall. Best-effort
    # — no API key / provider error degrades to FTS + display-name paths.
    try:
        from server.services.memory.embedding import embed_text, upsert_embedding
        embedding = await embed_text(f"{display_name}\n{body}")
        if embedding:
            await upsert_embedding(db, entity_id, embedding)
    except Exception:
        pass
    for key, value in claims:
        await db.execute(
            "INSERT INTO memory_claims (id, claim_type_key, subject_id, "
            "value, status, visibility, created_at) "
            "VALUES (?, ?, ?, ?, 'active', 'channel', "
            "strftime('%Y-%m-%dT%H:%M:%S','now'))",
            (str(uuid.uuid4()), key, entity_id, value))


async def cleanup_eval_fixtures(db: Any) -> None:
    """Remove every eval-prefixed entity, claim, and FTS row."""
    await db.execute(
        "DELETE FROM memory_claims WHERE subject_id LIKE 'eval-%'")
    await db.execute(
        "DELETE FROM memory_entities_fts WHERE entity_id LIKE 'eval-%'")
    try:
        await db.execute(
            "DELETE FROM memory_entity_embeddings WHERE entity_id LIKE "
            "'eval-%'")
    except Exception:
        pass  # vec table unavailable in some builds
    await db.execute(
        "DELETE FROM memory_entities WHERE entity_id LIKE 'eval-%'")
