"""Unified session service — conversation history for all channels."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from cyborg_server.services.base import BaseService

logger = logging.getLogger(__name__)


@dataclass
class SessionMessage:
    id: str
    session_key: str
    role: str
    content: str
    sender_id: str | None = None
    channel: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""


class SessionService(BaseService):
    """Manages conversation history across all channels."""

    async def add_message(
        self,
        session_key: str,
        role: str,
        content: str,
        *,
        channel: str | None = None,
        sender_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        dispatched: int = 1,
    ) -> str:
        """Store a message. Returns the message ID."""
        msg_id = str(uuid4())
        meta_json = json.dumps(metadata) if metadata else None
        await self.db.execute(
            """INSERT INTO session_messages
               (id, session_key, role, content, sender_id, channel, metadata, dispatched)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (msg_id, session_key, role, content, sender_id, channel, meta_json, dispatched),
        )
        return msg_id

    async def mark_dispatched(self, session_key: str) -> int:
        """Mark all undispatched user messages as dispatched. Returns count marked."""
        count = await self.db.execute(
            "UPDATE session_messages SET dispatched = 1 "
            "WHERE session_key = ? AND dispatched = 0 AND role = 'user'",
            (session_key,),
        )
        return count

    async def get_messages(
        self,
        session_key: str,
        *,
        limit: int = 50,
        roles: list[str] | None = None,
    ) -> list[SessionMessage]:
        """Retrieve messages for a session, oldest first."""
        if roles:
            placeholders = ",".join("?" for _ in roles)
            rows = await self.db.fetch_all(
                f"SELECT * FROM session_messages "
                f"WHERE session_key = ? AND role IN ({placeholders}) "
                f"ORDER BY created_at ASC LIMIT ?",
                (session_key, *roles, limit),
            )
        else:
            rows = await self.db.fetch_all(
                "SELECT * FROM session_messages "
                "WHERE session_key = ? ORDER BY created_at ASC LIMIT ?",
                (session_key, limit),
            )

        return [self._row_to_message(r) for r in rows]

    async def delete_session(self, session_key: str) -> None:
        """Delete all messages for a session."""
        await self.db.execute(
            "DELETE FROM session_messages WHERE session_key = ?",
            (session_key,),
        )

    def _row_to_message(self, row: Any) -> SessionMessage:
        meta = {}
        if row["metadata"]:
            try:
                meta = json.loads(row["metadata"])
            except (json.JSONDecodeError, TypeError):
                pass
        return SessionMessage(
            id=row["id"],
            session_key=row["session_key"],
            role=row["role"],
            content=row["content"],
            sender_id=row["sender_id"],
            channel=row["channel"],
            metadata=meta,
            created_at=row["created_at"],
        )
