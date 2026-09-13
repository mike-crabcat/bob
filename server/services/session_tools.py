"""Session tools — find sessions by name, read recent session messages."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from server.services.base import local_iso
from server.services.tools import Tool, tool

if TYPE_CHECKING:
    from server.context import AppContext

logger = logging.getLogger(__name__)

# Content cap per message: keeps a 50-message read bounded even when someone
# pastes an essay into the group.
_MAX_MESSAGE_CHARS = 2000


def _normalise_before(before: str) -> str | None:
    """Parse a paging cursor into canonical DB UTC ("YYYY-MM-DD HH:MM:SS"),
    exclusive. Accepts what local_iso returns ("2026-09-13 07:28:51+08:00"),
    bare ISO, and the raw DB format; returns None when unparseable. Naive
    strings are treated as UTC (the DB frame)."""
    s = before.strip()
    if not s:
        return ""
    from datetime import datetime, timezone

    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.fromisoformat(s) if fmt is None else datetime.strptime(s, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return None


def _snippet(content: str, query: str, *, before: int = 120, after: int = 240) -> str:
    """Window around the first case-insensitive match, single line."""
    idx = content.lower().find(query.lower())
    if idx < 0:
        return content[:after].replace("\n", " ")
    start = max(0, idx - before)
    snippet = content[start:idx + after].replace("\n", " ").strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if idx + after < len(content) else ""
    return f"{prefix}{snippet}{suffix}"


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
    ``bindings.contact_id``, so without this an untrusted dispatch
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
            from server.repositories.participants import ParticipantRepository
            keys |= set(await ParticipantRepository(db).conversations_for_contact(contact_id))
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
        from server.repositories.conversations import ConversationRepository
        rows = await ConversationRepository(db).named_sessions()

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
    async def get_session_messages(
        session_key: str = "", limit: int = 50, before: str = ""
    ) -> str:
        """Read messages from a session, oldest first. Defaults to the current session.
        Each message includes role, sender display name (when known), channel, and timestamp.
        Use this to check what was actually said — e.g. whether someone replied,
        confirmed, or declined — rather than relying on remembered status.
        Returns the newest `limit` messages (max 200). To read FURTHER BACK in
        history, pass `before` = the created_at timestamp of the OLDEST message
        from your previous read (pass it back exactly as returned) — you get the
        page preceding it. Repeat to walk back through the full history.
        (Messages in the same second as the boundary may be skipped — use
        search_session_messages when precision matters.)"""
        target = session_key.strip() or current_session_key
        if not target:
            return json.dumps({"error": "No session specified"})

        accessible_keys = await _accessible_session_keys()
        if accessible_keys is not None and target not in accessible_keys:
            return json.dumps({"error": "Session not accessible from this conversation"})

        if not 1 <= limit <= 200:
            limit = 50

        before_utc = _normalise_before(before)
        if before and before_utc is None:
            return json.dumps({
                "error": "Could not parse `before` — pass the exact created_at "
                          "string from a previous page, e.g. "
                          "'2026-09-13 07:28:51+08:00'."})

        # Newest N, then flip to oldest-first for readability. (SessionService
        # .get_messages applies LIMIT to the oldest end, which is wrong here.)
        from server.repositories.history import HistoryRepository
        messages = await HistoryRepository(db).recent_with_sender_names(
            target, limit=limit, before_utc=before_utc)

        return json.dumps({
            "session_key": target,
            "messages": [
                {
                    "role": m["role"],
                    "sender": m["sender_name"],
                    "channel": m["channel"],
                    "content": (m["content"] or "")[:_MAX_MESSAGE_CHARS],
                    "created_at": local_iso(m["created_at"]),
                }
                for m in messages
            ],
        })

    @tool
    async def search_session_messages(
        query: str, session_key: str = "", limit: int = 20
    ) -> str:
        """Search message HISTORY by content (case-insensitive substring), newest
        first. Each match includes session_key, conversation title, role, sender,
        timestamp, and a snippet around the match. Defaults to the current
        session; pass session_key from find_session to search elsewhere, or
        leave session_key as "all" to search every accessible conversation.
        Use this for "when did we discuss X" / "what did <person> say about X"
        over the full history — memory recall only holds what extraction kept,
        this reads what was actually said. Use get_session_messages(before=...)
        to read the context around a match."""
        q = query.strip()
        if not q:
            return json.dumps({"error": "Query cannot be empty"})
        if not 1 <= limit <= 50:
            limit = 20

        from server.repositories.history import HistoryRepository

        accessible_keys = await _accessible_session_keys()
        target: str | None
        if session_key.strip().lower() == "all":
            if accessible_keys is not None:
                # Untrusted: search only the accessible sessions, one by one —
                # the repo's global path has no session filter to clamp.
                rows: list[dict] = []
                for key in sorted(accessible_keys):
                    rows.extend(await HistoryRepository(db).search_messages_with_sender_names(
                        q, session_key=key, limit=limit))
                rows.sort(key=lambda m: (m["created_at"] or ""), reverse=True)
                matches = rows[:limit]
            else:
                matches = await HistoryRepository(db).search_messages_with_sender_names(
                    q, session_key=None, limit=limit)
        else:
            target = session_key.strip() or current_session_key
            if not target:
                return json.dumps({"error": "No session specified"})
            if accessible_keys is not None and target not in accessible_keys:
                return json.dumps({"error": "Session not accessible from this conversation"})
            matches = await HistoryRepository(db).search_messages_with_sender_names(
                q, session_key=target, limit=limit)

        # Resolve conversation_id → session_key + title for paging/context.
        titles, keys = await _conversation_labels({m["conversation_id"] for m in matches})

        return json.dumps({
            "query": q,
            "matches": [
                {
                    "session_key": keys.get(m["conversation_id"], m["conversation_id"]),
                    "conversation": titles.get(m["conversation_id"], ""),
                    "role": m["role"],
                    "sender": m["sender_name"],
                    "created_at": local_iso(m["created_at"]),
                    "snippet": _snippet(m["content"] or "", q),
                }
                for m in matches
            ],
        })

    async def _conversation_labels(cids: set[str]) -> tuple[dict[str, str], dict[str, str]]:
        """conversation_id → (title, session_key) via the conversations repo
        (bindings/conversations SQL stays owned by repositories)."""
        from server.repositories.conversations import ConversationRepository
        labels = await ConversationRepository(db).session_labels_for_cids(list(cids))
        titles = {cid: lab["title"] for cid, lab in labels.items()}
        keys = {cid: lab["session_key"] for cid, lab in labels.items()}
        return titles, keys

    return [find_session, get_session_messages, search_session_messages]
