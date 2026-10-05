"""Runs repository — owns all runs SQL.

One execution record per executor run (commitments plan Phase 0). Today the
writer is the backburner (kind 'flight'); history was backfilled from the
old detached_turn/script subagent rows with ids preserved.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

TERMINAL = ("completed", "failed", "killed")


class RunRepository:
    def __init__(self, db: Any):
        self.db = db

    async def start(
        self, *, kind: str, session_key: str, now_iso: str,
        run_id: str | None = None, dispatch_id: str | None = None,
        commitment_id: str | None = None, summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        rid = run_id or str(uuid4())
        await self.db.execute(
            """INSERT INTO runs (id, kind, session_key, dispatch_id,
               commitment_id, summary, status, metadata_json, started_at)
               VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)""",
            (rid, kind, session_key, dispatch_id, commitment_id, summary,
             json.dumps(metadata) if metadata else None, now_iso))
        return rid

    async def finish(
        self, run_id: str, *, status: str, now_iso: str,
        result: str | None = None, error: str | None = None,
    ) -> bool:
        """Terminal transition; only a running row moves (idempotent)."""
        n = await self.db.execute(
            """UPDATE runs SET status = ?, result = ?, error_message = ?,
               ended_at = ? WHERE id = ? AND status = 'running'""",
            (status, result, error, now_iso, run_id))
        return bool(n)

    async def get(self, run_id: str) -> dict[str, Any] | None:
        row = await self.db.fetch_one("SELECT * FROM runs WHERE id = ?", (run_id,))
        return _decode(row)

    async def get_by_prefix(self, prefix: str, *, kind: str) -> dict[str, Any] | None:
        """8-char ids are what the prompt renders; exactly one match or None."""
        rows = await self.db.fetch_all(
            "SELECT * FROM runs WHERE id LIKE ? AND kind = ? LIMIT 2",
            (prefix.strip() + "%", kind))
        return _decode(rows[0]) if rows and len(rows) == 1 else None

    async def running_for_session(self, session_key: str, *, kind: str) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            """SELECT * FROM runs WHERE session_key = ? AND kind = ?
               AND status = 'running' ORDER BY started_at""",
            (session_key, kind))
        return [_decode(r) for r in rows or []]

    async def fail_running(self, *, kind: str, now_iso: str, reason: str) -> list[dict[str, Any]]:
        """Boot sweep: every running row of this kind died with the process.
        Returns the rows it failed so the caller can own the loss."""
        rows = await self.db.fetch_all(
            "SELECT * FROM runs WHERE kind = ? AND status = 'running'", (kind,))
        await self.db.execute(
            """UPDATE runs SET status = 'failed', error_message = ?, ended_at = ?
               WHERE kind = ? AND status = 'running'""",
            (reason, now_iso, kind))
        return [_decode(r) for r in rows or []]

    async def running(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM runs WHERE status = 'running' "
            "ORDER BY started_at DESC LIMIT ?", (limit,))
        return [d for r in rows or [] if (d := _decode(r)) is not None]

    async def recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))
        return [_decode(r) for r in rows or []]


def _decode(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    try:
        d["metadata"] = json.loads(d.get("metadata_json") or "{}") or {}
    except (json.JSONDecodeError, TypeError):
        d["metadata"] = {}
    return d
