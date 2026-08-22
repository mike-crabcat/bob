"""Session tools — find sessions by name, read recent session messages."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from bob_server.services.tools import Tool, tool

if TYPE_CHECKING:
    from bob_server.context import AppContext

logger = logging.getLogger(__name__)

# Content cap per message: keeps a 50-message read bounded even when someone
# pastes an essay into the group.
_MAX_MESSAGE_CHARS = 2000


def make_session_tools(
    ctx: AppContext,
    *,
    is_trusted: bool = False,
    contact_id: str | None = None,
    session_key: str | None = None,
) -> list[Tool]:
    """Create session lookup/history tools bound to the given context.

    ``session_key`` is the session this dispatch is running in. It stays
    accessible even for untrusted contexts — group sessions have no
    ``session_routes.contact_id``, so without this an untrusted dispatch
    with no resolved contact (e.g. a routine run) could not see even the
    conversation it posts into.
    """

    db = ctx.db
    current_session_key = session_key

    async def _accessible_session_keys() -> set[str] | None:
        """Session keys this dispatch may see; None means unrestricted."""
        if is_trusted:
            return None
        keys: set[str] = set()
        if contact_id:
            rows = await db.fetch_all(
                "SELECT DISTINCT session_key FROM session_participants WHERE contact_id = ?",
                (contact_id,),
            )
            keys |= {r["session_key"] for r in rows}
        if current_session_key:
            keys.add(current_session_key)
        return keys

    @tool
    async def find_session(query: str, limit: int = 5) -> str:
        """Find a session by approximate name. Searches WhatsApp group names and contact names.
        Returns matching sessions with session_key, display name, kind, and channel.
        Only sessions this conversation can access are returned."""
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

        # Permission filter for untrusted contacts. The dispatch's own session
        # is always in the accessible set (see make_session_tools docstring).
        accessible_keys = await _accessible_session_keys()
        if accessible_keys is not None:
            rows = [r for r in rows if r["session_key"] in accessible_keys]

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
            from difflib import get_close_matches

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
    async def get_session_messages(session_key: str = "", limit: int = 50) -> str:
        """Read recent messages from a session, oldest first. Defaults to the current session.
        Each message includes role, sender display name (when known), channel, and timestamp.
        Use this to check what was actually said recently — e.g. whether someone replied,
        confirmed, or declined — rather than relying on remembered status."""
        target = session_key.strip() or current_session_key
        if not target:
            return json.dumps({"error": "No session specified"})

        accessible_keys = await _accessible_session_keys()
        if accessible_keys is not None and target not in accessible_keys:
            return json.dumps({"error": "Session not accessible from this conversation"})

        if not 1 <= limit <= 200:
            limit = 50

        # Newest N, then flip to oldest-first for readability. (SessionService
        # .get_messages applies LIMIT to the oldest end, which is wrong here.)
        from bob_server.repositories.history import HistoryRepository
        messages = await HistoryRepository(db).recent_with_sender_names(
            target, limit=limit)

        return json.dumps({
            "session_key": target,
            "messages": [
                {
                    "role": m["role"],
                    "sender": m["sender_name"],
                    "channel": m["channel"],
                    "content": (m["content"] or "")[:_MAX_MESSAGE_CHARS],
                    "created_at": m["created_at"],
                }
                for m in messages
            ],
        })

    return [find_session, get_session_messages]
