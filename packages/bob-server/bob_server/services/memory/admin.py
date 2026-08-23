"""Memory admin/query primitives — sanctioned home for memory_* SQL used by
tool wrappers, dashboards, heartbeat, and CLI (the memory package owns these
tables; callers outside services/memory/ must go through here).
"""

from __future__ import annotations

from typing import Any


# --------------------------------------------------------------- entities

async def get_active_entity(db: Any, entity_id: str) -> dict[str, Any] | None:
    row = await db.fetch_one(
        "SELECT entity_id, entity_type, display_name FROM memory_entities "
        "WHERE entity_id = ? AND status = 'active'",
        (entity_id,))
    return dict(row) if row else None


async def entity_exists(db: Any, entity_id: str) -> bool:
    row = await db.fetch_one(
        "SELECT 1 FROM memory_entities WHERE entity_id = ?", (entity_id,))
    return row is not None


async def archive_entity(db: Any, entity_id: str) -> None:
    await db.execute(
        "UPDATE memory_entities SET status = 'archived' WHERE entity_id = ?",
        (entity_id,))


async def rename_entity_row(
    db: Any, old_id: str, new_id: str, *, new_display_name: str | None = None,
) -> None:
    if new_display_name:
        await db.execute(
            "UPDATE memory_entities SET entity_id = ?, display_name = ? WHERE entity_id = ?",
            (new_id, new_display_name, old_id))
    else:
        await db.execute(
            "UPDATE memory_entities SET entity_id = ? WHERE entity_id = ?",
            (new_id, old_id))


async def purge_entity_index(db: Any, entity_id: str) -> None:
    """Drop FTS row + embedding for an entity_id that no longer exists."""
    await db.execute("DELETE FROM memory_entities_fts WHERE entity_id = ?", (entity_id,))
    await db.execute("DELETE FROM memory_entity_embeddings WHERE entity_id = ?", (entity_id,))


async def insert_entity(
    db: Any, entity_id: str, entity_type: str, display_name: str,
) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO memory_entities (entity_id, entity_type, display_name, status) "
        "VALUES (?, ?, ?, 'active')",
        (entity_id, entity_type, display_name))


async def all_active_entity_ids(db: Any) -> list[str]:
    rows = await db.fetch_all(
        "SELECT entity_id FROM memory_entities WHERE status = 'active'")
    return [r["entity_id"] for r in rows] if rows else []


async def active_entity_count(db: Any) -> int:
    row = await db.fetch_one(
        "SELECT COUNT(*) AS c FROM memory_entities WHERE status = 'active'")
    return (row["c"] if row else 0) or 0


async def entity_type_counts(db: Any) -> dict[str, int]:
    rows = await db.fetch_all(
        "SELECT entity_type, COUNT(*) AS count FROM memory_entities GROUP BY entity_type")
    return {r["entity_type"]: r["count"] for r in rows} if rows else {}


async def recent_entities(db: Any, *, limit: int = 50) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT e.entity_id, e.entity_type, e.display_name, e.updated_at, "
        " (SELECT COUNT(*) FROM memory_claims c WHERE c.subject_id = e.entity_id AND c.status = 'active') AS claim_count "
        "FROM memory_entities e ORDER BY e.updated_at DESC LIMIT ?",
        (limit,))
    return [dict(r) for r in rows] if rows else []


async def list_entities(db: Any, *, entity_type: str = "") -> list[dict[str, Any]]:
    query = (
        "SELECT e.entity_id, e.entity_type, e.display_name, e.status, e.updated_at, "
        "(SELECT COUNT(*) FROM memory_claims c WHERE c.subject_id = e.entity_id AND c.status = 'active') as claim_count "
        "FROM memory_entities e"
    )
    params: list[str] = []
    if entity_type:
        query += " WHERE e.entity_type = ?"
        params.append(entity_type)
    query += " ORDER BY e.updated_at DESC"
    rows = await db.fetch_all(query, tuple(params))
    return [dict(r) for r in rows] if rows else []


async def recently_touched_entity_ids(db: Any, *, limit: int) -> list[str]:
    """Entities with claim or entity rows created in the last 24h, most
    recently touched first — daily reconciliation candidates."""
    rows = await db.fetch_all(
        """
        SELECT entity_id, MAX(touched_at) AS last_touched FROM (
            SELECT subject_id AS entity_id, created_at AS touched_at
            FROM memory_claims
            WHERE status = 'active'
              AND datetime(created_at) > datetime('now', '-24 hours')
            UNION ALL
            SELECT entity_id, created_at AS touched_at
            FROM memory_entities
            WHERE status = 'active'
              AND datetime(created_at) > datetime('now', '-24 hours')
        )
        GROUP BY entity_id
        ORDER BY last_touched DESC
        LIMIT ?
        """,
        (limit,))
    return [r["entity_id"] for r in rows] if rows else []


# ----------------------------------------------------------------- claims

async def active_claim_ids(
    db: Any,
    *,
    subject_id: str | None = None,
    object_id: str | None = None,
    claim_type_key: str | None = None,
    value_or_object: str | None = None,
) -> list[str]:
    conditions = ["status = 'active'"]
    params: list[str] = []
    if subject_id:
        conditions.append("subject_id = ?")
        params.append(subject_id)
    if object_id:
        conditions.append("object_id = ?")
        params.append(object_id)
    if claim_type_key:
        conditions.append("claim_type_key = ?")
        params.append(claim_type_key)
    if value_or_object:
        conditions.append("(value = ? OR object_id = ?)")
        params.extend([value_or_object, value_or_object])
    rows = await db.fetch_all(
        f"SELECT id FROM memory_claims WHERE {' AND '.join(conditions)}",
        tuple(params))
    return [r["id"] for r in rows] if rows else []


async def supersede_claims(db: Any, claim_ids: list[str]) -> None:
    for cid in claim_ids:
        await db.execute(
            "UPDATE memory_claims SET status = 'superseded' WHERE id = ?", (cid,))


async def recent_claims_with_names(db: Any, *, limit: int = 10) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        """SELECT c.id, c.claim_type_key, c.subject_id, c.object_id, c.value,
                  c.created_at,
                  se.display_name AS subject_name,
                  se.entity_type  AS subject_type,
                  oe.display_name AS object_name,
                  oe.entity_type  AS object_type
           FROM memory_claims c
           LEFT JOIN memory_entities se ON se.entity_id = c.subject_id
           LEFT JOIN memory_entities oe ON oe.entity_id = c.object_id
           WHERE c.status = 'active'
           ORDER BY c.created_at DESC
           LIMIT ?""",
        (limit,))
    return [dict(r) for r in rows] if rows else []


async def claims_for_subjects(db: Any, entity_ids: list[str]) -> list[dict[str, Any]]:
    if not entity_ids:
        return []
    placeholders = ",".join("?" for _ in entity_ids)
    # Static table, dynamic placeholder count only.
    rows = await db.fetch_all(
        f"SELECT subject_id, claim_type_key, value, object_id FROM memory_claims "
        f"WHERE subject_id IN ({placeholders}) AND status = 'active'",
        tuple(entity_ids))
    return [dict(r) for r in rows] if rows else []


async def list_claims(
    db: Any,
    *,
    claim_type: str = "",
    subject_id: str = "",
    status: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    if claim_type:
        conditions.append("claim_type_key = ?")
        params.append(claim_type)
    if subject_id:
        conditions.append("subject_id = ?")
        params.append(subject_id)
    if status:
        conditions.append("status = ?")
        params.append(status)
    query = "SELECT * FROM memory_claims"
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = await db.fetch_all(query, tuple(params))
    return [dict(r) for r in rows] if rows else []


async def entity_id_for_contact_hex(db: Any, hex8: str) -> str | None:
    """Resolve the memory entity bound to a contact via its contact_id claim."""
    row = await db.fetch_one(
        "SELECT subject_id FROM memory_claims "
        "WHERE claim_type_key = 'contact_id' AND value = ? AND status = 'active' LIMIT 1",
        (hex8,))
    return row["subject_id"] if row else None


# ------------------------------------------------- questions / search log

async def list_questions(db: Any, *, status: str, limit: int = 100) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT * FROM memory_questions WHERE status = ? ORDER BY created_at DESC LIMIT ?",
        (status, limit))
    return [dict(r) for r in rows] if rows else []


async def list_search_log(db: Any, *, limit: int = 100) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT id, query, results_json, session_key, result_count, latency_seconds, created_at "
        "FROM memory_search_log ORDER BY created_at DESC LIMIT ?",
        (limit,))
    return [dict(r) for r in rows] if rows else []


async def insert_search_log(
    db: Any, *, log_id: str, query: str, results_json: str,
    session_key: str | None, result_count: int, latency_seconds: float,
) -> None:
    await db.execute(
        "INSERT INTO memory_search_log (id, query, results_json, session_key, result_count, latency_seconds) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (log_id, query, results_json, session_key, result_count, latency_seconds))
