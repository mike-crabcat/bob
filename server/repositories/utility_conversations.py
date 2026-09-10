"""SQL ownership for utility_conversations (docs/utility-conversations-plan.md).

Headless behaviors Bob requests and Mike approves: the charter is injected
per turn, the matching stimulus route gates firing. Everything touching the
table lives here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from server.database import Database

DEFAULT_MODEL_ALIAS = "cheap"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def utility_session_key(slug: str) -> str:
    return f"agent:{slug}:utility"


def is_utility_session(session_key: str) -> bool:
    return session_key.startswith("agent:") and session_key.endswith(":utility")


class UtilityConversationRepository:
    def __init__(self, db: Database):
        self.db = db

    async def get(self, session_key: str) -> dict[str, Any] | None:
        return await self.db.fetch_one(
            "SELECT * FROM utility_conversations WHERE session_key = ?",
            (session_key,))

    async def upsert(
        self, *, session_key: str, title: str, charter: str,
        model_alias: str = DEFAULT_MODEL_ALIAS, created_by: str = "",
    ) -> dict[str, Any]:
        now = _now_iso()
        await self.db.execute(
            "INSERT INTO utility_conversations "
            "(session_key, title, charter, model_alias, enabled, created_by, "
            " created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?) "
            "ON CONFLICT(session_key) DO UPDATE SET "
            "title = excluded.title, charter = excluded.charter, "
            "model_alias = excluded.model_alias, updated_at = excluded.updated_at",
            (session_key, title, charter, model_alias, created_by, now, now))
        return await self.get(session_key)  # type: ignore[return-value]

    async def set_enabled(self, session_key: str, enabled: bool) -> None:
        await self.db.execute(
            "UPDATE utility_conversations SET enabled = ?, updated_at = ? "
            "WHERE session_key = ?",
            (1 if enabled else 0, _now_iso(), session_key))

    async def delete(self, session_key: str) -> None:
        await self.db.execute(
            "DELETE FROM utility_conversations WHERE session_key = ?",
            (session_key,))

    async def list(self) -> list[dict[str, Any]]:
        return await self.db.fetch_all(
            "SELECT * FROM utility_conversations ORDER BY created_at")
