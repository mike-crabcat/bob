"""Session tools — find sessions by name."""

from __future__ import annotations

import json
import logging
from difflib import get_close_matches
from typing import TYPE_CHECKING

from bob_server.services.tools import Tool, tool

if TYPE_CHECKING:
    from bob_server.context import AppContext

logger = logging.getLogger(__name__)



def make_session_tools(
    ctx: AppContext,
    *,
    is_trusted: bool = False,
    contact_id: str | None = None,
) -> list[Tool]:
    """Create find_session tool bound to the given context."""

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

    return [find_session]
