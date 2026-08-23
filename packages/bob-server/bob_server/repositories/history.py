"""HistoryRepository — the single read path over session_messages (Bob3 Phase II).

Conversation-aware: every read is keyed by session_key (the binding key).
Prompt assembly, memory extraction, dreams, reflection, session tools and the
patience gate all read through here, so the Phase VI identity change (events
becoming the source of truth) has exactly one seam.

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
        since_clause = ""
        params: list[Any] = [session_key, session_key]
        if since_hours is not None:
            since_clause += " AND datetime(created_at) > datetime('now', ?) "
            params.append(f"-{since_hours} hours")
        if dispatched_only:
            since_clause += " AND dispatched = 1 "
        if pending_only:
            since_clause += " AND dispatched = 0 AND role = 'user' "
        params.append(limit)
        return await self.db.fetch_all(
            f"SELECT * FROM session_messages "
            f"WHERE session_key = ? AND role IN {_DIALOGUE_ROLES} "
            f"AND rowid IN (SELECT rowid FROM session_messages "
            f"WHERE session_key = ? AND role IN {_DIALOGUE_ROLES} "
            f"{internal}{since_clause} ORDER BY created_at DESC LIMIT ?) "
            f"ORDER BY created_at ASC",
            tuple(params),
        )

    async def count_dialogue(self, session_key: str, since_iso: str | None = None) -> int:
        if since_iso:
            row = await self.db.fetch_one(
                f"SELECT COUNT(*) AS n FROM session_messages "
                f"WHERE session_key = ? AND datetime(created_at) > datetime(?) "
                f"AND role IN {_DIALOGUE_ROLES}",
                (session_key, since_iso))
        else:
            row = await self.db.fetch_one(
                f"SELECT COUNT(*) AS n FROM session_messages "
                f"WHERE session_key = ? AND role IN {_DIALOGUE_ROLES}",
                (session_key,))
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
        if roles:
            placeholders = ",".join("?" for _ in roles)
            return await self.db.fetch_all(
                f"SELECT * FROM session_messages "
                f"WHERE session_key = ? AND role IN ({placeholders}) {internal}"
                f"ORDER BY created_at ASC LIMIT ?",
                (session_key, *roles, limit))
        return await self.db.fetch_all(
            f"SELECT * FROM session_messages "
            f"WHERE session_key = ? {internal}ORDER BY created_at ASC LIMIT ?",
            (session_key, limit))

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
        where = "session_key = ?"
        params: list[Any] = [session_key]
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
            f"SELECT * FROM session_messages WHERE {where} "
            f"ORDER BY created_at ASC LIMIT ?",
            tuple(params))

    async def last_message_at(self, session_key: str, *, role: str | None = None) -> str | None:
        if role:
            row = await self.db.fetch_one(
                "SELECT MAX(created_at) AS last_at FROM session_messages "
                "WHERE session_key = ? AND role = ?", (session_key, role))
        else:
            row = await self.db.fetch_one(
                "SELECT MAX(created_at) AS last_at FROM session_messages "
                "WHERE session_key = ?", (session_key,))
        return row["last_at"] if row else None

    async def has_any(self, session_key: str) -> bool:
        row = await self.db.fetch_one(
            "SELECT id FROM session_messages WHERE session_key = ? LIMIT 1",
            (session_key,))
        return row is not None

    async def recent_with_sender_names(self, session_key: str, *, limit: int) -> list[dict]:
        """Newest N joined to contact names, returned oldest-first. rowid
        breaks created_at ties (second granularity) by insertion order."""
        rows = await self.db.fetch_all(
            """SELECT sm.role, sm.content, sm.channel, sm.created_at, c.name AS sender_name
               FROM session_messages sm
               LEFT JOIN contacts c ON c.id = sm.sender_id AND c.deleted_at IS NULL
               WHERE sm.session_key = ?
               ORDER BY sm.created_at DESC, sm.rowid DESC LIMIT ?""",
            (session_key, limit))
        return list(reversed(rows or []))

    # ------------------------------------------------- dispatch claim/restore

    async def pending_user_ids(self, session_key: str) -> list[str]:
        rows = await self.db.fetch_all(
            "SELECT id FROM session_messages "
            "WHERE session_key = ? AND role = 'user' AND dispatched = 0",
            (session_key,))
        return [r["id"] for r in rows]

    async def restore_pending(self, message_ids: list[str]) -> None:
        """Undo a dispatch claim (e.g. after LLM quota failure)."""
        if not message_ids:
            return
        placeholders = ",".join("?" for _ in message_ids)
        await self.db.execute(
            f"UPDATE session_messages SET dispatched = 0 WHERE id IN ({placeholders})",
            tuple(message_ids))
