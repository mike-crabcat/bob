"""Memory seeding for eval worlds (server/evals/world.py).

Worlds are throwaway DBs, so there is nothing to clean up: the old
live-DB ``eval-`` prefix + cleanup path is retired (docs/evals.md). Seeds
go through the production write path (write_entity → write_claim →
FTS → embedding), so recall/find see them exactly as they see
extracted memory. The memory tables' SQL stays in this package
(test_sql_ownership owns the boundary).
"""

from __future__ import annotations

import contextvars
import re
import sqlite3
import uuid
from contextlib import contextmanager
from typing import Any

_ENTITY_REF = re.compile(r"^[a-z]+-[a-z0-9-]+$")

# Seeding writes go through production paths that embed on every entity
# write (update_entity_fts). The eval boundary (server/evals/intercept.py)
# honours this flag so seeding makes no per-entity embedding calls; worlds
# embed once, in a batch, after seeding.
SKIP_EMBEDDING: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "bob_eval_skip_embedding", default=False)


@contextmanager
def no_embedding():
    token = SKIP_EMBEDDING.set(True)
    try:
        yield
    finally:
        SKIP_EMBEDDING.reset(token)


async def seed_entity(ctx: Any, entity_id: str, entity_type: str,
                      display_name: str, claims: list[tuple[str, str]],
                      *, embed: bool = True) -> None:
    """Create an entity with claims. A claim value that is an entity
    reference (person-x, group-y) is stored as object_id."""
    from server.services.memory import MemoryService
    from server.services.memory.claim_service import (
        update_entity_fts, write_claim,
    )
    from server.services.memory.models import Claim, EntityDocument

    svc = MemoryService(ctx)
    with no_embedding():
        await svc.write_entity(ctx.settings.harness.workspace_dir, EntityDocument(
            entity_id=entity_id, entity_type=entity_type,
            display_name=display_name, status="active"))
        for key, value in claims:
            is_ref = bool(_ENTITY_REF.match(value)) and " " not in value
            await write_claim(ctx.db, Claim(
                id=f"claim-{uuid.uuid4().hex[:12]}", claim_type_key=key,
                subject_id=entity_id,
                object_id=value if is_ref else None,
                value=None if is_ref else value))
        await update_entity_fts(ctx.db, entity_id)
    if embed:
        await embed_entity(ctx.db, entity_id)


async def embed_entity(db: Any, entity_id: str) -> None:
    """Best-effort single-entity embedding (no key → FTS-only recall)."""
    try:
        from server.services.memory.claim_types import render_entity
        from server.services.memory.embedding import embed_text, upsert_embedding
        row = await db.fetch_one(
            "SELECT entity_type, display_name FROM memory_entities "
            "WHERE entity_id = ?", (entity_id,))
        if not row:
            return
        claims = await db.fetch_all(
            "SELECT claim_type_key, object_id, value FROM memory_claims "
            "WHERE status = 'active' AND subject_id = ?", (entity_id,))
        body = await render_entity(
            row["entity_type"], row["display_name"],
            [dict(c) for c in claims], entity_id=entity_id, db=db)
        vec = await embed_text(body)
        if vec:
            await upsert_embedding(db, entity_id, vec)
    except Exception:
        pass


def export_self_claims(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Self-bob's brief-whitelisted claims from an (operator-opened,
    read-only) production DB — the snapshot's self-knowledge, so world
    prompts carry the same learned practices production turns do."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT claim_type_key, value, created_at FROM memory_claims "
        "WHERE subject_id = 'self-bob' AND status = 'active' "
        "AND claim_type_key IN ('self_state', 'self_image', 'practice', 'limit') "
        "AND value IS NOT NULL ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]
