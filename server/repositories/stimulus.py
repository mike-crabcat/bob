"""SQL ownership for the stimulus spine tables (docs/stimulus-spine-plan.md).

Everything that touches stimulus_events / stimulus_routes lives here — the
ingest endpoint and the heartbeat router both go through this module.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from server.database import Database


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class StimulusRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def insert_event(
        self, *, source: str, type_: str, level: str, ts: str,
        dedup_key: str, ttl_s: int | None, target_hint: str | None,
        summary: str, body: dict[str, Any],
    ) -> tuple[int, bool]:
        """Append one event. ``dedup_key`` is required (the endpoint
        synthesises one when the source omits it). Returns (id, inserted);
        inserted=False means the dedup_key already existed (idempotent
        re-POST)."""
        import json
        rowcount = await self.db.execute(
            "INSERT OR IGNORE INTO stimulus_events "
            "(ts, source, type, level, dedup_key, ttl_s, target_hint, "
            " summary, body_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, source, type_, level, dedup_key, ttl_s, target_hint,
             summary, json.dumps(body, ensure_ascii=False)))
        row = await self.db.fetch_one(
            "SELECT id FROM stimulus_events WHERE dedup_key = ?", (dedup_key,))
        return (int(row["id"]) if row else 0), rowcount > 0

    async def pending_events(self) -> list[dict[str, Any]]:
        return await self.db.fetch_all(
            "SELECT * FROM stimulus_events WHERE processed_at IS NULL "
            "ORDER BY id")

    async def mark_processed(self, ids: list[int], outcome: str) -> None:
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        await self.db.execute(
            f"UPDATE stimulus_events SET processed_at = ?, delivered_steer = ? "
            f"WHERE id IN ({placeholders})",
            (utcnow_iso(), outcome, *ids))

    async def routes(self, *, enabled_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM stimulus_routes"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY priority, id"
        return await self.db.fetch_all(sql)

    async def prune_processed_before(self, cutoff_iso: str) -> int:
        rowcount = await self.db.execute(
            "DELETE FROM stimulus_events "
            "WHERE processed_at IS NOT NULL AND processed_at < ?",
            (cutoff_iso,))
        return int(rowcount or 0)
