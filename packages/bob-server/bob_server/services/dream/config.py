"""Runtime dream configuration.

Autoplan is SESSION-SCOPED: the per-session flag lives in session_routes.metadata
(same idiom as /patience and /verbose), settable from the chat itself via
/autoplan, the CLI, or the dashboard without a restart. The env setting
(BOB_DREAM_AUTO_APPROVE_PLANS) is only the boot default for sessions with no
explicit flag.
"""

from __future__ import annotations

import json
import logging

from bob_server.database import Database
from bob_server.services.base import iso_utc

logger = logging.getLogger(__name__)

ROUTE_META_KEY = "dream_autoplan"


async def get_session_autoplan(db: Database, session_key: str, boot_default: bool = False) -> bool:
    """Auto-approve state for one session; falls back to the boot default."""
    row = await db.fetch_one(
        "SELECT metadata FROM session_routes WHERE session_key = ? AND deleted_at IS NULL AND is_active = 1",
        (session_key,),
    )
    if row and row["metadata"]:
        try:
            meta = json.loads(row["metadata"])
        except (ValueError, TypeError):
            meta = {}
        if isinstance(meta, dict) and isinstance(meta.get(ROUTE_META_KEY), bool):
            return meta[ROUTE_META_KEY]
    return boot_default


async def set_session_autoplan(db: Database, session_key: str, enabled: bool) -> bool:
    """Set the per-session flag. Returns False when no active route exists."""
    row = await db.fetch_one(
        "SELECT id, metadata FROM session_routes WHERE session_key = ? AND deleted_at IS NULL AND is_active = 1",
        (session_key,),
    )
    if not row:
        return False
    try:
        meta = json.loads(row["metadata"]) if row["metadata"] else {}
    except (ValueError, TypeError):
        meta = {}
    meta[ROUTE_META_KEY] = bool(enabled)
    await db.execute(
        "UPDATE session_routes SET metadata = ?, updated_at = ? WHERE id = ?",
        (json.dumps(meta), iso_utc(), row["id"]),
    )
    return True


async def list_autoplan_sessions(db: Database, *, enabled: bool = True) -> list[dict]:
    """Sessions with an explicit autoplan flag, for status displays."""
    rows = await db.fetch_all(
        "SELECT session_key, metadata FROM session_routes "
        "WHERE deleted_at IS NULL AND is_active = 1 AND metadata LIKE ?",
        (f"%{ROUTE_META_KEY}%",),
    )
    out = []
    for r in rows or []:
        try:
            meta = json.loads(r["metadata"] or "{}")
        except (ValueError, TypeError):
            continue
        if meta.get(ROUTE_META_KEY) is enabled:
            out.append({"session_key": r["session_key"], "autoplan": meta[ROUTE_META_KEY]})
    return out
