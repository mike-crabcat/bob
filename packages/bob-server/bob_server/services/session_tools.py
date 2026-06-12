"""Session tools — find sessions by name and search memory bulletins."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from difflib import get_close_matches
from typing import TYPE_CHECKING

from bob_server.services.tools import Tool, tool

if TYPE_CHECKING:
    from bob_server.context import AppContext

logger = logging.getLogger(__name__)


def _parse_horizon(horizon: str) -> str | None:
    """Convert a natural language horizon to a datetime string for SQL comparison.

    Supports named horizons (today, this week, recent, this month, all)
    and numeric expressions like "2 days", "1 week", "3 hours".
    Returns None for 'all' (no time filter).
    """
    import re

    now = datetime.now()
    normalized = horizon.strip().lower()

    if normalized == "all":
        return None
    elif normalized == "today":
        bound = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif normalized in ("this week", "week"):
        bound = now - timedelta(days=7)
    elif normalized in ("this month", "month"):
        bound = now - timedelta(days=30)
    elif normalized in ("recent", "yesterday"):
        bound = now - timedelta(days=3)
    else:
        # Try "N unit" patterns: "2 days", "last 7 days", "1 week", "3 hours"
        m = re.match(r"(?:last\s+)?(\d+)\s*(day|week|hour|min|minute)s?", normalized)
        if m:
            n = int(m.group(1))
            unit = m.group(2)
            if unit == "day":
                bound = now - timedelta(days=n)
            elif unit == "week":
                bound = now - timedelta(weeks=n)
            elif unit in ("hour", "min", "minute"):
                bound = now - timedelta(hours=n if unit == "hour" else 0, minutes=n if unit.startswith("min") else 0)
            else:
                bound = now - timedelta(days=3)
        else:
            bound = now - timedelta(days=3)

    return bound.strftime("%Y-%m-%d %H:%M:%S")


def make_session_tools(
    ctx: AppContext,
    *,
    caller_session_key: str,
    is_trusted: bool = False,
    contact_id: str | None = None,
) -> list[Tool]:
    """Create find_session and search_bulletins tools bound to the given context."""

    db = ctx.db

    @tool
    async def find_session(query: str, limit: int = 5) -> str:
        """Find a session by approximate name. Searches WhatsApp group names and contact names.
        Returns matching sessions with session_key, display name, kind, and channel."""
        if not query.strip():
            return json.dumps({"error": "Query cannot be empty"})

        # Build name index: UNION of group sessions and DM sessions
        rows = await db.fetch_all(
            """
            SELECT sr.session_key, wg.name AS display_name, 'group' AS kind, sr.channel
            FROM session_routes sr
            JOIN whatsappgroups wg ON wg.whatsapp_jid = sr.chat_id AND wg.deleted_at IS NULL
            WHERE sr.deleted_at IS NULL AND sr.is_active = 1 AND sr.kind = 'group'
            UNION ALL
            SELECT sr.session_key, c.name AS display_name, 'dm' AS kind, sr.channel
            FROM session_routes sr
            JOIN contacts c ON c.id = sr.contact_id AND c.deleted_at IS NULL
            WHERE sr.deleted_at IS NULL AND sr.is_active = 1 AND sr.kind = 'dm'
            """
        )

        if not rows:
            return json.dumps({"matches": [], "message": "No sessions found"})

        # Permission filter for untrusted contacts
        if not is_trusted and contact_id:
            accessible = await db.fetch_all(
                "SELECT DISTINCT session_key FROM session_participants WHERE contact_id = ?",
                (contact_id,),
            )
            accessible_keys = {r["session_key"] for r in accessible}
            rows = [r for r in rows if r["session_key"] in accessible_keys]
        elif not is_trusted:
            rows = []

        if not rows:
            return json.dumps({"matches": [], "message": "No accessible sessions found"})

        # Two-phase matching: substring then fuzzy
        query_lower = query.strip().lower()
        candidates = [(r["display_name"].lower(), r) for r in rows if r["display_name"]]

        # Phase 1: substring matches
        substring_matches = [
            (name, row) for name, row in candidates if query_lower in name
        ]

        if substring_matches:
            matched = substring_matches[:limit]
        else:
            # Phase 2: fuzzy similarity
            names = [name for name, _ in candidates]
            close = get_close_matches(query_lower, names, n=limit, cutoff=0.4)
            name_to_rows: dict[str, list] = {}
            for name, row in candidates:
                name_to_rows.setdefault(name, []).append(row)
            matched = []
            for name in close:
                matched.extend((name, r) for r in name_to_rows[name])
            matched = matched[:limit]

        results = [
            {
                "session_key": row["session_key"],
                "display_name": row["display_name"],
                "kind": row["kind"],
                "channel": row["channel"],
            }
            for _, row in matched
        ]
        return json.dumps({"matches": results})

    @tool
    async def search_bulletins(
        session_key: str = "",
        horizon: str = "recent",
        query: str = "",
        limit: int = 10,
    ) -> str:
        """Search memory bulletins by session and time horizon.
        Horizon options: 'today', 'this week', 'recent' (3 days), 'this month', 'all'.
        Defaults to the current session. Use '*' to search across all sessions.
        Use query to filter bulletins by keyword."""
        raw = session_key.strip()
        search_all = raw == "*"

        # Resolve which session to query
        if search_all:
            effective_key = None
        elif raw:
            effective_key = raw
        else:
            effective_key = caller_session_key

        # Parse horizon
        bound = _parse_horizon(horizon)

        # Permission check for untrusted callers
        accessible_keys: set[str] | None = None
        if not is_trusted:
            if search_all:
                if not contact_id:
                    return json.dumps({"error": "No access to search all sessions"})
                accessible = await db.fetch_all(
                    "SELECT DISTINCT session_key FROM session_participants WHERE contact_id = ?",
                    (contact_id,),
                )
                accessible_keys = {r["session_key"] for r in accessible}
            elif effective_key and effective_key != caller_session_key:
                # Querying a session other than the caller's own — check participant
                if contact_id:
                    participant = await db.fetch_one(
                        "SELECT 1 FROM session_participants WHERE session_key = ? AND contact_id = ?",
                        (effective_key, contact_id),
                    )
                    if not participant:
                        return json.dumps({"error": "No access to this session"})
                else:
                    return json.dumps({"error": "No access to this session"})

        # Build query
        conditions: list[str] = []
        params: list = []

        if effective_key:
            conditions.append("source_id = ?")
            params.append(effective_key)

        if bound is not None:
            conditions.append("created_at >= ?")
            params.append(bound)

        if query.strip():
            conditions.append("content LIKE ?")
            params.append(f"%{query.strip()}%")

        where = " AND ".join(conditions) if conditions else "1=1"
        params.append(limit)

        rows = await db.fetch_all(
            f"SELECT id, created_at, source_id, channel_id, source_type, visibility, content "
            f"FROM memory_bulletins WHERE {where} "
            f"ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        )

        # Filter by accessible sessions for untrusted cross-session search
        if accessible_keys is not None:
            rows = [r for r in rows if r["source_id"] in accessible_keys]

        bulletins = []
        for r in rows:
            content = r["content"] or ""
            preview = content[:200] + ("..." if len(content) > 200 else "")
            bulletins.append({
                "id": r["id"],
                "created_at": r["created_at"],
                "source_id": r["source_id"],
                "content_preview": preview,
                "content_length": len(content),
            })

        return json.dumps({
            "total": len(bulletins),
            "horizon": horizon,
            "session_key": effective_key if effective_key else "*",
            "bulletins": bulletins,
        })

    return [find_session, search_bulletins]
