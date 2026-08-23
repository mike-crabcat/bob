"""Participants and agendas keyed by canonical conversation_id (Increment 6a).

Callers still speak session_key at the seam; resolution goes through
bindings (falling back to the key itself — conversations.id equals the
legacy session_key 1:1), so merged conversations share one participant
roster and one agenda.
"""

from __future__ import annotations

from typing import Any

from bob_server.database import Database

# Resolves a session_key to its canonical conversation id inline: the
# binding's conversation if one exists, else the key itself.
CID = "COALESCE((SELECT conversation_id FROM bindings WHERE session_key = ?), ?)"


class ParticipantRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def _cid(self, session_key: str, *, create: bool = False) -> str:
        row = await self.db.fetch_one(
            "SELECT conversation_id FROM bindings WHERE session_key = ?",
            (session_key,))
        if row:
            return row["conversation_id"]
        if create:
            from bob_server.repositories.conversations import ConversationRepository

            conv = await ConversationRepository(self.db).ensure(session_key)
            return conv["id"]
        return session_key

    async def upsert(
        self,
        session_key: str,
        identifier: str,
        *,
        display_name: str = "",
        contact_id: str | None = None,
        is_trusted: bool = False,
        now_iso: str,
    ) -> None:
        """Standard participant upsert: a non-empty display_name wins,
        contact_id fills in but never clears, trust only updates when the
        row carries a resolved contact."""
        cid = await self._cid(session_key, create=True)
        await self.db.execute(
            """INSERT INTO participants (conversation_id, identifier, display_name, contact_id, is_trusted, last_active_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(conversation_id, identifier) DO UPDATE SET
                   display_name = CASE WHEN excluded.display_name != '' THEN excluded.display_name ELSE participants.display_name END,
                   contact_id = COALESCE(excluded.contact_id, participants.contact_id),
                   is_trusted = CASE WHEN excluded.contact_id IS NOT NULL THEN excluded.is_trusted ELSE participants.is_trusted END,
                   last_active_at = excluded.last_active_at""",
            (cid, identifier, display_name, contact_id,
             1 if is_trusted else 0, now_iso))

    async def touch(
        self, session_key: str, identifier: str, display_name: str, now_iso: str,
    ) -> None:
        """Presence bump: insert if unknown, otherwise only refresh
        last_active_at (mention resolution path)."""
        cid = await self._cid(session_key, create=True)
        await self.db.execute(
            """INSERT INTO participants (conversation_id, identifier, display_name, contact_id, is_trusted, last_active_at)
               VALUES (?, ?, ?, NULL, 0, ?)
               ON CONFLICT(conversation_id, identifier) DO UPDATE SET
                   last_active_at = excluded.last_active_at""",
            (cid, identifier, display_name, now_iso))

    async def list_for(self, session_key: str) -> list[dict[str, Any]]:
        return await self.db.fetch_all(
            f"""SELECT conversation_id, identifier, display_name, contact_id,
                       is_trusted, last_active_at
                FROM participants
                WHERE conversation_id = {CID}
                ORDER BY last_active_at DESC""",
            (session_key, session_key))

    async def get(self, session_key: str, identifier: str) -> dict[str, Any] | None:
        return await self.db.fetch_one(
            f"SELECT * FROM participants WHERE conversation_id = {CID} AND identifier = ?",
            (session_key, session_key, identifier))

    async def conversations_for_contact(self, contact_id: str) -> list[str]:
        rows = await self.db.fetch_all(
            "SELECT DISTINCT conversation_id FROM participants WHERE contact_id = ?",
            (contact_id,))
        return [r["conversation_id"] for r in rows]

    async def contact_session_rollup(self, contact_id: str) -> list[dict[str, Any]]:
        """A contact's conversations with LLM-call counts, most recent first."""
        rows = await self.db.fetch_all(
            """SELECT sp.conversation_id AS session_key, sp.last_active_at,
                      (SELECT COUNT(*) FROM llm_call_log l WHERE l.session_key = sp.conversation_id) as call_count
               FROM participants sp
               WHERE sp.contact_id = ?
               ORDER BY sp.last_active_at DESC""",
            (contact_id,))
        return [dict(r) for r in rows] if rows else []


class AgendaRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, session_key: str) -> str | None:
        row = await self.db.fetch_one(
            f"SELECT agenda FROM agendas WHERE conversation_id = {CID}",
            (session_key, session_key))
        return row["agenda"] if row and row["agenda"] else None

    async def set(self, session_key: str, agenda: str, now_iso: str) -> None:
        cid_row = await self.db.fetch_one(
            "SELECT conversation_id FROM bindings WHERE session_key = ?",
            (session_key,))
        if cid_row:
            cid = cid_row["conversation_id"]
        else:
            from bob_server.repositories.conversations import ConversationRepository

            conv = await ConversationRepository(self.db).ensure(session_key)
            cid = conv["id"]
        await self.db.execute(
            """INSERT INTO agendas (conversation_id, agenda, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(conversation_id) DO UPDATE SET
                   agenda = excluded.agenda, updated_at = excluded.updated_at""",
            (cid, agenda, now_iso))
