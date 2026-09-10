"""SQL ownership for the stimulus spine tables (docs/stimulus-spine-plan.md).

Everything that touches stimulus_events / stimulus_routes lives here — the
ingest endpoint and the heartbeat router both go through this module.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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

    async def get_route(self, route_id: int) -> dict[str, Any] | None:
        return await self.db.fetch_one(
            "SELECT * FROM stimulus_routes WHERE id = ?", (route_id,))

    async def insert_route(
        self, *, source: str, type_pattern: str, level: str,
        target_session: str, hours: str | None = None,
        cooldown_s: int | None = None, budget_per_hour: int | None = None,
        note: str = "", created_by: str = "",
    ) -> int:
        """Insert a route, disabled (utility-conversations plan Part 3: the
        request tool writes inert; approval flips it live)."""
        now = utcnow_iso()
        await self.db.execute(
            "INSERT INTO stimulus_routes "
            "(source, type_pattern, level, target_session, enabled, priority, "
            " note, created_at, created_by, hours, cooldown_s, budget_per_hour) "
            "VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?)",
            (source, type_pattern, level, target_session, note, now, created_by,
             hours, cooldown_s, budget_per_hour))
        row = await self.db.fetch_one("SELECT last_insert_rowid() AS id")
        return int(row["id"]) if row else 0

    async def update_route(
        self, route_id: int, *, source: str | None = None,
        type_pattern: str | None = None, level: str | None = None,
        hours: str | None = None, cooldown_s: int | None = None,
        budget_per_hour: int | None = None, note: str | None = None,
        enabled: bool | None = None,
    ) -> None:
        """Update route columns. ``None`` leaves a column unchanged; valves
        are cleared by passing the empty string / 0 explicitly."""
        sets, params = [], []
        if source is not None:
            sets.append("source = ?")
            params.append(source)
        if type_pattern is not None:
            sets.append("type_pattern = ?")
            params.append(type_pattern)
        if level is not None:
            sets.append("level = ?")
            params.append(level)
        if enabled is not None:
            sets.append("enabled = ?")
            params.append(1 if enabled else 0)
        if hours is not None:
            sets.append("hours = ?")
            params.append(hours or None)
        if cooldown_s is not None:
            sets.append("cooldown_s = ?")
            params.append(cooldown_s or None)
        if budget_per_hour is not None:
            sets.append("budget_per_hour = ?")
            params.append(budget_per_hour or None)
        if note is not None:
            sets.append("note = ?")
            params.append(note)
        if not sets:
            return
        params.append(route_id)
        await self.db.execute(
            f"UPDATE stimulus_routes SET {', '.join(sets)} WHERE id = ?",
            tuple(params))

    async def set_route_enabled(self, route_id: int, enabled: bool,
                                note: str | None = None) -> bool:
        rowcount = await self.db.execute(
            "UPDATE stimulus_routes SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, route_id))
        if note is not None:
            await self.db.execute(
                "UPDATE stimulus_routes SET note = ? WHERE id = ?",
                (note, route_id))
        return bool(rowcount)

    async def delete_route(self, route_id: int) -> None:
        await self.db.execute("DELETE FROM stimulus_routes WHERE id = ?", (route_id,))
        await self.db.execute(
            "DELETE FROM stimulus_route_fires WHERE route_id = ?", (route_id,))

    async def delete_route_if_inert(self, route_id: int) -> bool:
        """Delete only while disabled — a live route was approved later; a
        stale rejection must not kill the behavior. Returns True if deleted."""
        rowcount = await self.db.execute(
            "DELETE FROM stimulus_routes WHERE id = ? AND enabled = 0",
            (route_id,))
        if rowcount:
            await self.db.execute(
                "DELETE FROM stimulus_route_fires WHERE route_id = ?",
                (route_id,))
        return bool(rowcount)

    async def has_live_route(self, target_session: str) -> bool:
        row = await self.db.fetch_one(
            "SELECT id FROM stimulus_routes "
            "WHERE target_session = ? AND enabled = 1", (target_session,))
        return row is not None

    async def stamp_route_ids(self, ids: list[int], route_id: int) -> None:
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        await self.db.execute(
            f"UPDATE stimulus_events SET route_id = ? WHERE id IN ({placeholders})",
            (route_id, *ids))

    async def record_fires(self, route_ids: list[int], ts_iso: str) -> None:
        """One fire row per route that delivered a steer (valve state)."""
        for rid in set(route_ids):
            await self.db.execute(
                "INSERT INTO stimulus_route_fires (route_id, ts) VALUES (?, ?)",
                (rid, ts_iso))

    async def fire_stats(self, route_id: int, *, window_s: int = 3600) -> dict[str, Any]:
        """Valve inputs: last fire time (router clock) and fires inside the
        rolling window, off stimulus_route_fires so multi-route events count
        for every delivering route."""
        row = await self.db.fetch_one(
            "SELECT MAX(ts) AS last_fire FROM stimulus_route_fires "
            "WHERE route_id = ?", (route_id,))
        last = row["last_fire"] if row else None
        if last:
            cutoff = (datetime.now(timezone.utc)
                      - timedelta(seconds=window_s)).isoformat()
            n = await self.db.fetch_one(
                "SELECT COUNT(*) AS c FROM stimulus_route_fires "
                "WHERE route_id = ? AND ts > ?", (route_id, cutoff))
            count = int(n["c"]) if n else 0
        else:
            count = 0
        return {"last_fire": last, "fires_window": count}

    async def prune_fires_before(self, cutoff_iso: str) -> int:
        rowcount = await self.db.execute(
            "DELETE FROM stimulus_route_fires WHERE ts < ?", (cutoff_iso,))
        return int(rowcount or 0)

    async def prune_processed_before(self, cutoff_iso: str) -> int:
        rowcount = await self.db.execute(
            "DELETE FROM stimulus_events "
            "WHERE processed_at IS NOT NULL AND processed_at < ?",
            (cutoff_iso,))
        return int(rowcount or 0)
