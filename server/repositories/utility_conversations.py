"""SQL ownership for utility_conversations (docs/utility-conversations-plan.md).

Headless behaviors Bob requests and Mike approves: the charter is injected
per turn, the matching stimulus route gates firing. Everything touching the
table lives here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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

    async def set_report_to(self, session_key: str,
                            report_to: str | None) -> None:
        """Where this conversation's concerning output goes (004). Owner-set
        in v1 — the self-serve tool doesn't take a report_to param yet, since
        a new destination is a widening change that deserves its own review."""
        await self.db.execute(
            "UPDATE utility_conversations SET report_to = ?, updated_at = ? "
            "WHERE session_key = ?",
            (report_to, _now_iso(), session_key))

    async def delete(self, session_key: str) -> None:
        await self.db.execute(
            "DELETE FROM utility_conversations WHERE session_key = ?",
            (session_key,))

    async def list(self) -> list[dict[str, Any]]:
        return await self.db.fetch_all(
            "SELECT * FROM utility_conversations ORDER BY created_at")

    async def dashboard_overview(self) -> list[dict[str, Any]]:
        """Ops-card rollup (cross-domain read-only, the
        ConversationRepository.dashboard_overview precedent): each utility
        conversation LEFT JOINed with its routes, valve state, recent fire
        counts and last turn. The chats list hides utility sessions (plan
        Part 1 "invisible to the chats list; shown under ops") — this query
        is the ops view."""
        cutoff_24h = (datetime.now(timezone.utc)
                      - timedelta(hours=24)).isoformat()
        return [dict(r) for r in await self.db.fetch_all(
            """
            SELECT u.session_key, u.title, u.charter, u.model_alias,
                   u.enabled, u.report_to, u.created_by, u.updated_at,
                   (SELECT MAX(l.created_at) FROM llm_call_log l
                    WHERE l.session_key = u.session_key) AS last_turn_at,
                   r.id AS route_id, r.source, r.type_pattern, r.level,
                   r.enabled AS route_enabled, r.hours, r.cooldown_s,
                   r.budget_per_hour, r.note AS route_note,
                   (SELECT COUNT(*) FROM stimulus_route_fires f
                    WHERE f.route_id = r.id AND f.ts > ?) AS fires_24h,
                   (SELECT MAX(f.ts) FROM stimulus_route_fires f
                    WHERE f.route_id = r.id) AS last_fire_at
            FROM utility_conversations u
            LEFT JOIN stimulus_routes r ON r.target_session = u.session_key
            ORDER BY u.created_at, r.id
            """, (cutoff_24h,))]
