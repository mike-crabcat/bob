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

from server.database import Database


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _kind_of(session_key: str) -> str:
    if ":group:" in session_key:
        return "group"
    if ":dm:" in session_key:
        return "dm"
    if ":thread:" in session_key:
        return "thread"
    return "internal"


def wa_send_jid(address: str | None) -> str | None:
    """WhatsApp send target from a binding address: JIDs pass through
    (groups, legacy DM chat_ids); phone numbers become @s.whatsapp.net."""
    if not address:
        return None
    if "@" in address:
        return address
    digits = "".join(ch for ch in address if ch.isdigit())
    return f"{digits}@s.whatsapp.net" if digits else None


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

    async def resolve_cid(self, session_key: str, txn: Any = None) -> str:
        """Canonical conversation id for a session key (identity when unbound;
        conversation ids are legacy session_keys 1:1 until merges)."""
        row = await (txn or self.db).fetch_one(
            "SELECT conversation_id FROM bindings WHERE session_key = ?",
            (session_key,))
        return row["conversation_id"] if row else session_key

    async def active_binding(self, session_key: str) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            "SELECT channel, endpoint_kind, address, contact_id FROM bindings "
            "WHERE session_key = ? AND is_active = 1",
            (session_key,))
        return dict(row) if row else None

    async def bindings_for_many(
        self, conversation_ids: list[str],
    ) -> list[dict[str, Any]]:
        if not conversation_ids:
            return []
        marks = ",".join("?" * len(conversation_ids))
        rows = await self.db.fetch_all(
            f"""SELECT conversation_id, session_key, channel, kind, address,
                       merged_from, merged_at
                FROM bindings WHERE conversation_id IN ({marks})""",
            tuple(conversation_ids))
        return [dict(r) for r in rows]

    async def named_sessions(self) -> list[dict[str, Any]]:
        """Active bindings with a human display name: group sessions named by
        the WhatsApp group, DM sessions by the bound contact."""
        rows = await self.db.fetch_all(
            """
            SELECT b.session_key, wg.name AS display_name, 'group' AS kind, b.channel
            FROM bindings b
            JOIN whatsappgroups wg ON wg.whatsapp_jid = b.address AND wg.deleted_at IS NULL
            WHERE b.is_active = 1 AND b.endpoint_kind = 'group'
            UNION ALL
            SELECT b.session_key, c.name AS display_name, 'dm' AS kind, b.channel
            FROM bindings b
            JOIN contacts c ON c.id = b.contact_id AND c.deleted_at IS NULL
            WHERE b.is_active = 1 AND b.endpoint_kind = 'dm'
            """)
        return [dict(r) for r in rows] if rows else []

    async def contact_name_for(self, session_key: str) -> str | None:
        row = await self.db.fetch_one(
            "SELECT c.name FROM bindings b "
            "JOIN contacts c ON c.id = b.contact_id AND c.deleted_at IS NULL "
            "WHERE b.session_key = ?",
            (session_key,))
        return row["name"] if row else None

    async def dashboard_overview(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Conversation list rollup for the dashboard (cross-domain read-only)."""
        rows = await self.db.fetch_all(
            """SELECT c.id, c.kind, c.title, c.merged_into, c.updated_at,
                      (SELECT COUNT(*) FROM bindings b WHERE b.conversation_id = c.id) AS binding_count,
                      (SELECT MAX(t.created_at) FROM turns t WHERE t.conversation_id = c.id) AS last_turn_at,
                      (SELECT replace(MAX(l.created_at), ' ', 'T') FROM llm_call_log l
                       WHERE l.session_key = c.id) AS last_llm_at,
                      (SELECT COUNT(*) FROM turns t WHERE t.conversation_id = c.id) AS turn_count,
                      (SELECT COUNT(*) FROM goals g WHERE g.conversation_id = c.id AND g.status = 'active') AS active_goals
               FROM conversations c
               ORDER BY COALESCE(NULLIF(MAX(COALESCE(last_turn_at, ''), COALESCE(last_llm_at, '')), ''), c.updated_at) DESC
               LIMIT ?""", (limit,))
        return [dict(r) for r in rows] if rows else []

    async def group_memory_entity_id(self, session_key: str) -> str | None:
        """Memory entity id for a group session's bound WhatsApp group."""
        row = await self.db.fetch_one(
            "SELECT wg.memory_entity_id FROM whatsappgroups wg "
            "JOIN bindings b ON b.address = wg.whatsapp_jid "
            "WHERE b.session_key = ? AND wg.deleted_at IS NULL",
            (session_key,))
        return row["memory_entity_id"] if row and row["memory_entity_id"] else None

    async def route_for(self, session_key: str) -> dict[str, Any] | None:
        """THE routing resolver (Increment 4): everything the legacy
        session_routes read sites need, from the binding map. Returns
        {conversation_id, channel, endpoint_kind, address, contact_id,
        is_active} or None for unknown keys."""
        row = await self.db.fetch_one(
            """SELECT conversation_id, channel, endpoint_kind, address,
                      contact_id, is_active
               FROM bindings WHERE session_key = ?""",
            (session_key,))
        return dict(row) if row else None

    async def get_policy(self, session_key: str) -> dict[str, Any]:
        """The conversation's policy flags (patience_*, dream_autoplan, …),
        resolved through the binding map. {} when unset/unknown."""
        import json as _json
        conv = await self.resolve(session_key) or await self.get(session_key)
        if not conv or not conv.get("policy_json"):
            return {}
        try:
            policy = _json.loads(conv["policy_json"])
        except (ValueError, TypeError):
            return {}
        return policy if isinstance(policy, dict) else {}

    async def set_policy(self, session_key: str, updates: dict[str, Any]) -> bool:
        """Merge flag updates into the conversation's policy_json. Returns
        False when the session_key resolves to no conversation."""
        import json as _json
        conv = await self.resolve(session_key) or await self.get(session_key)
        if not conv:
            return False
        policy = await self.get_policy(conv["id"])
        policy.update(updates)
        await self.db.execute(
            "UPDATE conversations SET policy_json = ?, updated_at = ? WHERE id = ?",
            (_json.dumps(policy), _now_iso(), conv["id"]))
        return True

    async def ensure(
        self,
        session_key: str,
        *,
        title: str | None = None,
        address: str | None = None,
        endpoint_kind: str | None = None,
    ) -> dict[str, Any]:
        """Get-or-create the 1:1 conversation + binding for a session_key
        (the lazy path for session_keys created after the backfill).
        When the caller knows the wire address / endpoint kind, they are
        stored on the binding — and filled in on existing bindings that
        predate this knowledge (never overwritten)."""
        existing = await self.resolve(session_key)
        if existing:
            if address or endpoint_kind:
                await self.db.execute(
                    """UPDATE bindings SET address = COALESCE(address, ?),
                              endpoint_kind = COALESCE(endpoint_kind, ?)
                       WHERE session_key = ?""",
                    (address, endpoint_kind, session_key))
            return existing
        now = _now_iso()
        await self.db.execute(
            """INSERT OR IGNORE INTO conversations (id, kind, title, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (session_key, _kind_of(session_key), title, now, now))
        await self.db.execute(
            """INSERT OR IGNORE INTO bindings
               (session_key, conversation_id, channel, kind, address, endpoint_kind, created_at)
               VALUES (?, ?, ?, 'thread', ?, ?, ?)""",
            (session_key, session_key, _channel_of(session_key),
             address, endpoint_kind, now))
        return (await self.resolve(session_key))  # type: ignore[return-value]

    async def bind(
        self,
        session_key: str,
        conversation_id: str,
        *,
        channel: str,
        kind: str = "thread",
        address: str | None = None,
        endpoint_kind: str | None = None,
    ) -> None:
        """Attach an additional binding to an EXISTING conversation — e.g. a
        voice call endpoint bound to the person's conversation (Phase VI
        item 5). Idempotent; never creates a conversation."""
        await self.db.execute(
            """INSERT OR IGNORE INTO bindings
               (session_key, conversation_id, channel, kind, address, endpoint_kind, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (session_key, conversation_id, channel, kind, address,
             endpoint_kind, _now_iso()))

    async def register_endpoint(
        self,
        session_key: str,
        *,
        endpoint_kind: str,
        address: str | None = None,
        contact_id: str | None = None,
    ) -> None:
        """Ensure the conversation + binding for a channel endpoint carries
        routing truth (address, endpoint_kind, contact_id). This replaced
        session_routes.create_route (Increment 4): channel ingress paths call
        it whenever a message arrives on an endpoint. Idempotent; fills in
        blanks on existing bindings, never overwrites."""
        if not address and contact_id:
            c = await self.db.fetch_one(
                "SELECT phone_number, email FROM contacts WHERE id = ?",
                (str(contact_id),))
            if c:
                address = c["phone_number"] or c["email"]
        await self.ensure(session_key, address=address, endpoint_kind=endpoint_kind)
        if contact_id:
            await self.db.execute(
                """UPDATE bindings SET contact_id = COALESCE(contact_id, ?), is_active = 1
                   WHERE session_key = ?""",
                (str(contact_id), session_key))

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
        from server.repositories.event_log import Event, EventLogRepository
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
