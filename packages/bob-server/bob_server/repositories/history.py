"""HistoryRepository — the single read path over messages (Bob3 Phase II).

Conversation-aware: callers pass a session_key (binding key); every read
resolves it to the canonical conversation_id via bindings (falling back to
the key itself — conversations.id equals the legacy session_key 1:1), so
merged conversations read as one history. Prompt assembly, memory
extraction, dreams, reflection, session tools and the patience gate all
read through here, so identity changes have exactly one seam.

Writes (add_message / mark_dispatched / delete) stay in SessionService for
now; the claim/restore pair used by dispatch lives here because it is a read-
modify cycle over the same rows.
"""

from __future__ import annotations

from typing import Any

_DIALOGUE_ROLES = "('user', 'assistant')"

# Internal bookkeeping rows excluded from replayed context by default.
# wake_nudge and routine provenance ARE replayed — they are the actual
# conversational stimulus for their turns.
_INTERNAL_FILTER = (
    " AND (provenance IS NULL OR provenance NOT IN "
    "('extraction_marker', 'dream_announcement')) "
)


class HistoryRepository:
    def __init__(self, db: Any):
        self.db = db

    async def _cid(self, session_key: str) -> str:
        row = await self.db.fetch_one(
            "SELECT conversation_id FROM bindings WHERE session_key = ?",
            (session_key,))
        return row["conversation_id"] if row else session_key

    async def recent_dialogue(
        self,
        session_key: str,
        *,
        limit: int,
        since_hours: float | None = None,
        dispatched_only: bool = False,
        pending_only: bool = False,
        include_internal: bool = False,
    ) -> list[dict]:
        """Last N user/assistant rows, oldest-first (full columns).

        The newest-N-then-chronological shape used by prompt assembly and
        memory extraction: LIMIT applies to the newest end, output is ASC.
        Internal bookkeeping rows (extraction markers, dream announcements)
        are excluded unless ``include_internal`` is set.
        """
        internal = "" if include_internal else _INTERNAL_FILTER
        cid = await self._cid(session_key)
        since_clause = ""
        params: list[Any] = [cid, cid]
        if since_hours is not None:
            since_clause += " AND datetime(created_at) > datetime('now', ?) "
            params.append(f"-{since_hours} hours")
        if dispatched_only:
            since_clause += " AND dispatched = 1 "
        if pending_only:
            since_clause += " AND dispatched = 0 AND role = 'user' "
        params.append(limit)
        return await self.db.fetch_all(
            f"SELECT * FROM messages "
            f"WHERE conversation_id = ? AND role IN {_DIALOGUE_ROLES} "
            f"AND rowid IN (SELECT rowid FROM messages "
            f"WHERE conversation_id = ? AND role IN {_DIALOGUE_ROLES} "
            f"{internal}{since_clause} ORDER BY created_at DESC LIMIT ?) "
            f"ORDER BY created_at ASC",
            tuple(params),
        )

    async def count_dialogue(self, session_key: str, since_iso: str | None = None) -> int:
        cid = await self._cid(session_key)
        if since_iso:
            row = await self.db.fetch_one(
                f"SELECT COUNT(*) AS n FROM messages "
                f"WHERE conversation_id = ? AND datetime(created_at) > datetime(?) "
                f"AND role IN {_DIALOGUE_ROLES}",
                (cid, since_iso))
        else:
            row = await self.db.fetch_one(
                f"SELECT COUNT(*) AS n FROM messages "
                f"WHERE conversation_id = ? AND role IN {_DIALOGUE_ROLES}",
                (cid,))
        return int(row["n"]) if row and row["n"] else 0

    async def messages(
        self, session_key: str, *, limit: int = 50, roles: list[str] | None = None,
        include_internal: bool = True,
    ) -> list[dict]:
        """Oldest-first slice (LIMIT applies to the OLDEST end — legacy
        SessionService.get_messages semantics, preserved as-is). Defaults to
        including internal rows: existing callers are ops/inspection surfaces
        (dashboard, reflection) that want the full record."""
        internal = "" if include_internal else _INTERNAL_FILTER
        cid = await self._cid(session_key)
        if roles:
            placeholders = ",".join("?" for _ in roles)
            return await self.db.fetch_all(
                f"SELECT * FROM messages "
                f"WHERE conversation_id = ? AND role IN ({placeholders}) {internal}"
                f"ORDER BY created_at ASC LIMIT ?",
                (cid, *roles, limit))
        return await self.db.fetch_all(
            f"SELECT * FROM messages "
            f"WHERE conversation_id = ? {internal}ORDER BY created_at ASC LIMIT ?",
            (cid, limit))

    async def messages_since(
        self,
        session_key: str,
        *,
        since_iso: str | None = None,
        lookback_days: int | None = None,
        role: str | None = None,
        limit: int = 50,
        include_internal: bool = False,
    ) -> list[dict]:
        """Chronological messages after a cursor timestamp or within a lookback."""
        where = "conversation_id = ?"
        params: list[Any] = [await self._cid(session_key)]
        if not include_internal:
            where += _INTERNAL_FILTER
        if role:
            where += " AND role = ?"
            params.append(role)
        if since_iso:
            where += " AND datetime(created_at) > datetime(?)"
            params.append(since_iso)
        elif lookback_days is not None:
            where += " AND datetime(created_at) > datetime('now', ?)"
            params.append(f"-{lookback_days} days")
        params.append(limit)
        return await self.db.fetch_all(
            f"SELECT * FROM messages WHERE {where} "
            f"ORDER BY created_at ASC LIMIT ?",
            tuple(params))

    async def last_message_at(self, session_key: str, *, role: str | None = None) -> str | None:
        cid = await self._cid(session_key)
        if role:
            row = await self.db.fetch_one(
                "SELECT MAX(created_at) AS last_at FROM messages "
                "WHERE conversation_id = ? AND role = ?", (cid, role))
        else:
            row = await self.db.fetch_one(
                "SELECT MAX(created_at) AS last_at FROM messages "
                "WHERE conversation_id = ?", (cid,))
        return row["last_at"] if row else None

    async def has_any(self, session_key: str) -> bool:
        row = await self.db.fetch_one(
            "SELECT id FROM messages WHERE conversation_id = ? LIMIT 1",
            (await self._cid(session_key),))
        return row is not None

    async def recent_with_sender_names(self, session_key: str, *, limit: int) -> list[dict]:
        """Newest N joined to contact names, returned oldest-first. rowid
        breaks created_at ties (second granularity) by insertion order."""
        rows = await self.db.fetch_all(
            """SELECT sm.role, sm.content, sm.channel, sm.created_at, c.name AS sender_name
               FROM messages sm
               LEFT JOIN contacts c ON c.id = sm.sender_id AND c.deleted_at IS NULL
               WHERE sm.conversation_id = ?
               ORDER BY sm.created_at DESC, sm.rowid DESC LIMIT ?""",
            (await self._cid(session_key), limit))
        return list(reversed(rows or []))

    # ------------------------------------------------- dispatch claim/restore

    async def pending_user_ids(self, session_key: str) -> list[str]:
        rows = await self.db.fetch_all(
            "SELECT id FROM messages "
            "WHERE conversation_id = ? AND role = 'user' AND dispatched = 0",
            (await self._cid(session_key),))
        return [r["id"] for r in rows]

    async def restore_pending(self, message_ids: list[str]) -> None:
        """Undo a dispatch claim (e.g. after LLM quota failure)."""
        if not message_ids:
            return
        placeholders = ",".join("?" for _ in message_ids)
        await self.db.execute(
            f"UPDATE messages SET dispatched = 0 WHERE id IN ({placeholders})",
            tuple(message_ids))
