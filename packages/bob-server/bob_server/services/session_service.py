"""Unified session service — conversation history for all channels."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from bob_server.services.base import BaseService

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
        dispatch_id: str | None = None,
        synthetic: bool | None = None,
    ) -> str:
        """Store a message. Returns the message ID.

        When ``synthetic`` is None and ``role == "assistant"``, the flag is
        auto-detected from the dispatch's memory-tool usage via
        ``LLMDispatchService.pop_memory_used(dispatch_id)``. Explicit values
        are honoured as-is.

        When ``role == "assistant"`` and ``dispatch_id`` is set, the dispatch's
        tool-call trace is also pulled via ``pop_tool_trace`` and persisted to
        ``tool_summary`` / ``tool_blocks_json`` for replay in future dispatches.
        """
        tool_summary: str | None = None
        tool_blocks_json: str | None = None
        if synthetic is None:
            if role == "assistant" and dispatch_id:
                from bob_server.services.llm_dispatch import LLMDispatchService
                synthetic = LLMDispatchService.pop_memory_used(dispatch_id)
                trace = LLMDispatchService.pop_tool_trace(dispatch_id)
                if trace is not None:
                    tool_summary = trace["summary"] or None
                    tool_blocks_json = trace["items_json"]
            else:
                synthetic = False
        msg_id = str(uuid4())
        meta_json = json.dumps(metadata) if metadata else None
        await self.db.execute(
            """INSERT INTO session_messages
               (id, session_key, role, content, sender_id, channel, metadata,
                dispatched, synthetic, tool_summary, tool_blocks_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (msg_id, session_key, role, content, sender_id, channel, meta_json,
             dispatched, 1 if synthetic else 0, tool_summary, tool_blocks_json),
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
