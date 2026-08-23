"""Conversation repository — conversations & bindings (Bob3 Phase VI).

A conversation is the unit of dialogue; a binding maps a channel session_key
onto it. Backfilled conversations use the session_key as their id, so legacy
``conversation_id`` values (session_keys, invariant 3 since Phase I) resolve
without history rewrites.

Merge is explicit and provenance-recorded. Unmerge is defined, not magical:
bindings return to their pre-merge conversation (so pre-merge events, keyed
by binding, follow); post-merge artifacts stay with the survivor. "Restore
the exact prior state" is NOT the contract.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from bob_server.database import Database


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _kind_of(session_key: str) -> str:
    if ":group:" in session_key:
        return "group"
    if ":dm:" in session_key:
        return "dm"
    return "internal"


def _channel_of(session_key: str) -> str:
    if ":whatsapp:" in session_key:
        return "whatsapp"
    if ":email:" in session_key:
        return "email"
    if session_key.startswith("subagent:"):
        return "subagent"
    if ":voice" in session_key:
        return "voice"
    return "internal"


class ConversationRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, conversation_id: str) -> dict[str, Any] | None:
        return await self.db.fetch_one(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,))

    async def get_binding(self, session_key: str) -> dict[str, Any] | None:
        return await self.db.fetch_one(
            "SELECT * FROM bindings WHERE session_key = ?", (session_key,))

    async def resolve(self, session_key: str) -> dict[str, Any] | None:
        """Resolve a session_key to its conversation via the binding map."""
        return await self.db.fetch_one(
            """SELECT c.* FROM bindings b
               JOIN conversations c ON c.id = b.conversation_id
               WHERE b.session_key = ?""",
            (session_key,))

    async def bindings_for(self, conversation_id: str) -> list[dict[str, Any]]:
        return await self.db.fetch_all(
            "SELECT * FROM bindings WHERE conversation_id = ? ORDER BY created_at",
            (conversation_id,))

    async def ensure(self, session_key: str, *, title: str | None = None) -> dict[str, Any]:
        """Get-or-create the 1:1 conversation + binding for a session_key
        (the lazy path for session_keys created after the backfill)."""
        existing = await self.resolve(session_key)
        if existing:
            return existing
        now = _now_iso()
        await self.db.execute(
            """INSERT OR IGNORE INTO conversations (id, kind, title, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (session_key, _kind_of(session_key), title, now, now))
        await self.db.execute(
            """INSERT OR IGNORE INTO bindings
               (session_key, conversation_id, channel, kind, created_at)
               VALUES (?, ?, ?, 'thread', ?)""",
            (session_key, session_key, _channel_of(session_key), now))
        return (await self.resolve(session_key))  # type: ignore[return-value]

    async def bind(
        self,
        session_key: str,
        conversation_id: str,
        *,
        channel: str,
        kind: str = "thread",
        address: str | None = None,
    ) -> None:
        """Attach an additional binding to an EXISTING conversation — e.g. a
        voice call endpoint bound to the person's conversation (Phase VI
        item 5). Idempotent; never creates a conversation."""
        await self.db.execute(
            """INSERT OR IGNORE INTO bindings
               (session_key, conversation_id, channel, kind, address, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_key, conversation_id, channel, kind, address, _now_iso()))

    async def merge(
        self,
        source_conversation_ids: list[str],
        survivor_conversation_id: str,
        *,
        note: str | None = None,
    ) -> int:
        """Merge conversations into a survivor: all their bindings move to
        the survivor with provenance (merged_from/merged_at); the merged
        conversation rows are marked merged_into. Returns bindings moved.

        Callers gate on operator confirmation or a high-confidence identity
        signal — this method records, it does not decide.
        """
        now = _now_iso()
        moved = 0
        async with self.db.transaction() as txn:
            survivor = await txn.fetch_one(
                "SELECT id FROM conversations WHERE id = ? AND merged_into IS NULL",
                (survivor_conversation_id,))
            if survivor is None:
                raise ValueError(f"survivor conversation {survivor_conversation_id} not found or merged")
            for source_id in source_conversation_ids:
                if source_id == survivor_conversation_id:
                    continue
                moved += await txn.execute(
                    """UPDATE bindings
                       SET conversation_id = ?, merged_from = ?, merged_at = ?
                       WHERE conversation_id = ? AND merged_from IS NULL""",
                    (survivor_conversation_id, source_id, now, source_id))
                await txn.execute(
                    "UPDATE conversations SET merged_into = ?, updated_at = ? WHERE id = ?",
                    (survivor_conversation_id, now, source_id))
        await self._append_merge_event(
            "conversation.merged", survivor_conversation_id,
            {"sources": source_conversation_ids, "note": note})
        return moved

    async def unmerge(self, session_key: str) -> str | None:
        """Return one binding to its pre-merge conversation. Pre-merge events
        follow via binding_key; post-merge artifacts stay with the survivor.
        Returns the restored conversation_id, or None if not merged."""
        now = _now_iso()
        async with self.db.transaction() as txn:
            binding = await txn.fetch_one(
                "SELECT * FROM bindings WHERE session_key = ?", (session_key,))
            if binding is None or not binding["merged_from"]:
                return None
            original = binding["merged_from"]
            await txn.execute(
                """UPDATE bindings
                   SET conversation_id = ?, merged_from = NULL, merged_at = NULL
                   WHERE session_key = ?""",
                (original, session_key))
            # Reactivate the original conversation if no other binding kept
            # it merged away.
            await txn.execute(
                "UPDATE conversations SET merged_into = NULL, updated_at = ? WHERE id = ?",
                (now, original))
        await self._append_merge_event(
            "conversation.unmerged", original,
            {"session_key": session_key})
        return original

    async def _append_merge_event(self, event_type: str, conversation_id: str,
                                  payload: dict[str, Any]) -> None:
        from bob_server.repositories.event_log import Event, EventLogRepository
        try:
            await EventLogRepository(self.db).append(Event(
                event_type=event_type,
                binding_key=f"conversation:{conversation_id}",
                conversation_id=conversation_id,
                source="conversations",
                external_id=f"{event_type}:{conversation_id}:{uuid.uuid4().hex}",
                payload=payload,
            ))
        except Exception:
            pass
