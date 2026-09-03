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
# wake_nudge provenance IS replayed — it is the actual conversational
# stimulus for its turn. Routine REPLIES replay (the group saw them, so
# later turns must know what was posted); routine PROMPTS never do —
# routine turns build self-contained context (routine_service passes
# session_key="") and the prompt rows otherwise leak the routine's to-do
# list into other turns' context (2026-08-29: a recovery turn adopted two
# routine agendas and ran a 10-minute tool odyssey).
_INTERNAL_FILTER = (
    " AND (provenance IS NULL OR provenance NOT IN "
    "('extraction_marker', 'dream_announcement')) "
    # IS NOT is NULL-safe equality: plain user rows have NULL provenance,
    # and `NOT (provenance = 'routine' AND role = 'user')` would evaluate
    # to NULL for them and silently drop them from replay.
    " AND (provenance IS NOT 'routine' OR role IS NOT 'user') "
)


class HistoryRepository:
    def __init__(self, db: Any):
        self.db = db

    async def _cid(self, session_key: str) -> str:
        from server.repositories.conversations import ConversationRepository
        return await ConversationRepository(self.db).resolve_cid(session_key)

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
        # rowid breaks created_at ties (second granularity) by insertion
        # order — batched same-second arrivals must replay in the order they
        # arrived, or prompt assembly sees them scrambled.
        return await self.db.fetch_all(
            f"SELECT * FROM messages "
            f"WHERE conversation_id = ? AND role IN {_DIALOGUE_ROLES} "
            f"AND rowid IN (SELECT rowid FROM messages "
            f"WHERE conversation_id = ? AND role IN {_DIALOGUE_ROLES} "
            f"{internal}{since_clause} ORDER BY created_at DESC, rowid DESC LIMIT ?) "
            f"ORDER BY created_at ASC, rowid ASC",
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

    async def probe_reaction_within(self, session_key: str, minutes: float) -> bool:
        """Has the attention probe posted a reaction clip in this session
        within the window? The STAND_DOWN reaction tier's per-chat cooldown —
        reaction rows are assistant messages recorded with provenance
        'probe_reaction' and a '[reaction] ' content prefix."""
        cid = await self._cid(session_key)
        row = await self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM messages "
            "WHERE conversation_id = ? AND role = 'assistant' "
            "AND content LIKE '[reaction] %' "
            "AND datetime(created_at) > datetime('now', ?)",
            (cid, f"-{minutes} minutes"))
        return bool(row and row["n"])

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

    async def message_by_id(self, message_id: str) -> dict | None:
        """Single message row by primary key (idempotency/lookups by id)."""
        return await self.db.fetch_one(
            "SELECT * FROM messages WHERE id = ?", (message_id,))

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

    async def messages_by_ids(self, message_ids: list[str]) -> list[dict]:
        """Raw message rows by id (id, conversation_id, created_at) — the
        entity-mention index resolves claim provenance through here (Bob
        Events §2.1). Missing ids simply don't resolve."""
        if not message_ids:
            return []
        marks = ",".join("?" for _ in message_ids)
        rows = await self.db.fetch_all(
            f"SELECT id, conversation_id, created_at FROM messages "
            f"WHERE id IN ({marks})", tuple(message_ids))
        return [dict(r) for r in rows] if rows else []

    async def pending_user_ids(self, session_key: str) -> list[str]:
        rows = await self.db.fetch_all(
            "SELECT id FROM messages "
            "WHERE conversation_id = ? AND role = 'user' AND dispatched = 0",
            (await self._cid(session_key),))
        return [r["id"] for r in rows]

    async def has_undispatched_inbound(self, session_key: str) -> bool:
        """Whether any undispatched user row is a raw human message.

        Inbound WhatsApp rows are stored with no provenance label; wake-path
        rows always carry one (wake_nudge / steer / task_relay). The bridge's
        tool assembly reads this to tell turns a human started (get
        steer_conversation) from autonomous wake turns (get the proactive
        group-send tool) — same NULL-vs-labelled distinction the send-tool
        rescue uses via claimed_provenances.
        """
        row = await self.db.fetch_one(
            "SELECT 1 FROM messages "
            "WHERE conversation_id = ? AND role = 'user' AND dispatched = 0 "
            "AND provenance IS NULL LIMIT 1",
            (await self._cid(session_key),))
        return row is not None

    async def claimed_provenances(self, message_ids: list[str]) -> list[str]:
        """Provenance values of the messages a dispatch claimed (NULL → "").

        The send-tool rescue reads this to tell turns triggered by a real
        inbound message (expected to speak) from system-nudge-only turns
        (silence is a legitimate outcome).
        """
        if not message_ids:
            return []
        marks = ",".join("?" for _ in message_ids)
        rows = await self.db.fetch_all(
            f"SELECT provenance FROM messages WHERE id IN ({marks})",
            tuple(message_ids))
        return [str(r["provenance"] or "") for r in rows]

    async def restore_pending(self, message_ids: list[str]) -> None:
        """Undo a dispatch claim (e.g. after LLM quota failure)."""
        if not message_ids:
            return
        placeholders = ",".join("?" for _ in message_ids)
        await self.db.execute(
            f"UPDATE messages SET dispatched = 0 WHERE id IN ({placeholders})",
            tuple(message_ids))

    async def restore_messages_for_turn(self, turn_id: str) -> int:
        """Restore dispatch claims held by a (zombie) turn so a retry has
        something to claim. Joins turn_events/event_log read-only to find
        the message ids the turn's events reference."""
        return await self.db.execute(
            """UPDATE messages SET dispatched = 0
               WHERE role = 'user' AND id IN (
                 SELECT json_extract(e.payload_json, '$.session_message_id')
                 FROM turn_events te JOIN event_log e ON e.id = te.event_id
                 WHERE te.turn_id = ?
                   AND json_extract(e.payload_json, '$.session_message_id') IS NOT NULL)""",
            (turn_id,))

    async def count_since(self, *, role: str, channel: str, since_iso: str) -> int:
        row = await self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM messages "
            "WHERE role = ? AND channel = ? AND created_at >= ?",
            (role, channel, since_iso))
        return int(row["n"]) if row else 0

    async def undispatched_count(self, *, hours: int = 48) -> int:
        row = await self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM messages "
            "WHERE role = 'user' AND dispatched = 0 "
            f"AND created_at >= datetime('now', '-{int(hours)} hours')")
        return int(row["n"]) if row else 0

    async def undispatched_conversations(self, *, channel: str) -> list[str]:
        """Conversations holding stored-but-unclaimed user messages (crash recovery)."""
        rows = await self.db.fetch_all(
            "SELECT DISTINCT conversation_id FROM messages "
            "WHERE role = 'user' AND dispatched = 0 AND channel = ?",
            (channel,))
        return [r["conversation_id"] for r in rows]

    # ----------------------------------------------------- one-off lookups

    async def content_by_id(self, message_id: str | None) -> str | None:
        if not message_id:
            return None
        row = await self.db.fetch_one(
            "SELECT content FROM messages WHERE id = ?", (message_id,))
        return row["content"] if row else None

    async def first_assistant_after(self, session_key: str, since_iso: str) -> str | None:
        row = await self.db.fetch_one(
            "SELECT content FROM messages "
            "WHERE conversation_id = ? AND role = 'assistant' AND created_at >= ? "
            "ORDER BY created_at LIMIT 1",
            (await self._cid(session_key), since_iso))
        return row["content"] if row else None

    async def window_before(
        self, session_key: str, before_iso: str, *, limit: int = 6,
    ) -> list[dict]:
        """The last N messages at-or-before a timestamp, oldest-first."""
        rows = await self.db.fetch_all(
            "SELECT role, content FROM messages "
            "WHERE conversation_id = ? AND created_at <= ? "
            "ORDER BY created_at DESC LIMIT ?",
            (await self._cid(session_key), before_iso, limit))
        return [dict(r) for r in reversed(rows or [])]

    async def assistant_replied_between(
        self, session_key: str, after_iso: str, *, window_minutes: int,
    ) -> bool:
        row = await self.db.fetch_one(
            """SELECT 1 AS x FROM messages
               WHERE conversation_id = ?
                 AND role = 'assistant'
                 AND datetime(created_at) > datetime(?)
                 AND datetime(created_at) <= datetime(?, ?)
               LIMIT 1""",
            (await self._cid(session_key), after_iso, after_iso,
             f"+{window_minutes} minutes"))
        return row is not None

    # -------------------------------------------- cross-conversation rollups
    # These read other domains' cursor tables (memory_extraction_turns,
    # dream_session_review) read-only — sanctioned for repositories.

    async def activity_rollup(self, *, limit: int = 50) -> list[dict]:
        rows = await self.db.fetch_all(
            """SELECT conversation_id AS session_key,
                      COUNT(*) as msg_count,
                      MAX(created_at) || 'Z' as last_activity
               FROM messages
               GROUP BY conversation_id
               ORDER BY last_activity DESC
               LIMIT ?""", (limit,))
        return [dict(r) for r in rows] if rows else []

    async def extraction_candidates(self, *, idle_threshold_minutes: float) -> list[dict]:
        """Conversations with messages newer than their last silent memory
        extraction, idle past the threshold (heartbeat idle-summary seam)."""
        rows = await self.db.fetch_all(
            """
            SELECT
                sm.conversation_id AS session_key,
                MAX(sm.created_at) AS last_message_at,
                COALESCE(
                    (SELECT MAX(ran_at) FROM memory_extraction_turns
                     WHERE session_key = sm.conversation_id),
                    '1970-01-01'
                ) AS active_from,
                COUNT(*) AS message_count
            FROM messages sm
            WHERE sm.conversation_id NOT LIKE 'subagent:%'
              AND datetime(sm.created_at) > datetime(COALESCE(
                (SELECT MAX(ran_at) FROM memory_extraction_turns
                 WHERE session_key = sm.conversation_id),
                '1970-01-01'
              ))
            GROUP BY sm.conversation_id
            HAVING datetime(MAX(sm.created_at)) < datetime('now', '-' || ? || ' minutes')
            """,
            (idle_threshold_minutes,))
        return [dict(r) for r in rows] if rows else []

    async def review_candidates(
        self, *, min_new_messages: int, max_sessions: int, first_run_lookback_days: int,
    ) -> list[dict]:
        """Conversations with messages after their dream-review cursor
        (dream idle-review seam)."""
        rows = await self.db.fetch_all(
            """
            SELECT
                sm.conversation_id AS session_key,
                MAX(sm.created_at) AS newest_message_at,
                COUNT(*) AS new_messages,
                COALESCE(dsr.last_reviewed_message_at, '') AS cursor_at
            FROM messages sm
            LEFT JOIN dream_session_review dsr ON dsr.session_key = sm.conversation_id
            WHERE sm.conversation_id NOT LIKE 'subagent:%'
              AND datetime(sm.created_at) > datetime(COALESCE(dsr.last_reviewed_message_at, '1970-01-01'))
              AND (
                dsr.session_key IS NOT NULL
                OR datetime(sm.created_at) > datetime('now', ?)
              )
            GROUP BY sm.conversation_id
            HAVING COUNT(*) >= ?
            ORDER BY newest_message_at DESC
            LIMIT ?
            """,
            (f"-{first_run_lookback_days} days", min_new_messages, max_sessions))
        return [dict(r) for r in rows] if rows else []

    async def assistant_metadata_count_today(self, session_key: str, *, metadata_like: str) -> int:
        row = await self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM messages "
            "WHERE conversation_id = ? AND role = 'assistant' "
            "AND metadata LIKE ? AND date(created_at) = date('now')",
            (await self._cid(session_key), metadata_like))
        return int(row["n"]) if row else 0

    async def recent_assistant_with_metadata(self, *, metadata_like: str, limit: int) -> list[dict]:
        rows = await self.db.fetch_all(
            "SELECT conversation_id AS session_key, content, created_at FROM messages "
            "WHERE role = 'assistant' AND metadata LIKE ? "
            "ORDER BY created_at DESC LIMIT ?",
            (metadata_like, limit))
        return [dict(r) for r in rows] if rows else []

    async def detail_messages(self, session_key: str, *, limit: int = 200) -> list[dict]:
        """Dashboard detail view: newest N with resolved sender names
        (contacts, falling back to the participant roster), oldest-first."""
        rows = await self.db.fetch_all(
            "SELECT sm.id, sm.role, sm.content, sm.channel, sm.sender_id, sm.created_at, "
            "COALESCE(c.name, sp.display_name) as sender_name "
            "FROM messages sm "
            "LEFT JOIN contacts c ON c.id = sm.sender_id AND c.deleted_at IS NULL "
            "LEFT JOIN participants sp ON sp.contact_id = sm.sender_id "
            "AND sp.conversation_id = sm.conversation_id "
            "WHERE sm.rowid IN ("
            "  SELECT rowid FROM messages WHERE conversation_id = ?"
            "  ORDER BY created_at DESC LIMIT ?"
            ") ORDER BY sm.created_at ASC",
            (await self._cid(session_key), limit))
        return [dict(r) for r in rows] if rows else []
