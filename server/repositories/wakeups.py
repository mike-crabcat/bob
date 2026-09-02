"""Wakeup repository — scheduled conversation wakes (Bob3 Phase V).

A wakeup is a durable "wake this conversation at/after T" row. The heartbeat
pump claims due wakeups (CAS scheduled→fired) and wakes the conversation.
Goal deadlines schedule wakeups; completing/cancelling a goal cancels its
outstanding wakeups.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from bob_server.database import Database


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_instant(value: str) -> datetime | None:
    """Parse an ISO instant from a not_before/deadline string, tolerating a
    trailing 'Z'. Returns None when unparseable."""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class WakeupRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def schedule(
        self,
        *,
        conversation_id: str,
        not_before: str,
        goal_id: str | None = None,
        recurrence: str | None = None,
        tz: str | None = None,
        created_by_turn: str | None = None,
        kind: str = "wake",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        wid = str(uuid.uuid4())
        await self.db.execute(
            """INSERT INTO wakeups
               (id, conversation_id, goal_id, not_before, recurrence, tz,
                status, created_by_turn, created_at, kind, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, ?, ?)""",
            (wid, conversation_id, goal_id, not_before, recurrence, tz,
             created_by_turn, _now_iso(), kind, json.dumps(payload or {})),
        )
        return (await self.get(wid))  # type: ignore[return-value]

    async def get(self, wakeup_id: str) -> dict[str, Any] | None:
        return await self.db.fetch_one("SELECT * FROM wakeups WHERE id = ?", (wakeup_id,))

    async def cancel(self, wakeup_id: str) -> bool:
        count = await self.db.execute(
            "UPDATE wakeups SET status = 'cancelled' "
            "WHERE id = ? AND status = 'scheduled'",
            (wakeup_id,),
        )
        return bool(count)

    async def cancel_for_goal(self, goal_id: str) -> int:
        return await self.db.execute(
            "UPDATE wakeups SET status = 'cancelled' "
            "WHERE goal_id = ? AND status = 'scheduled'",
            (goal_id,),
        )

    async def cancel_for_routine(self, routine_id: str) -> int:
        return await self.db.execute(
            "UPDATE wakeups SET status = 'cancelled' "
            "WHERE kind = 'routine' AND status = 'scheduled' "
            "AND json_extract(payload_json, '$.routine_id') = ?",
            (routine_id,),
        )

    async def claim_due(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Claim due wakeups by CAS scheduled→fired one at a time; only rows
        this caller flipped are returned, so two pumps never double-fire.

        ``not_before`` values are compared as parsed instants, not strings:
        goal-deadline wakeups carry whatever offset the deadline was written
        with (e.g. ``+08:00``), which sorts ~8h late against the UTC ``now``
        string (found live 2026-08-31 — the coffee goal's deadline wake)."""
        now = datetime.now(timezone.utc)
        candidates = await self.db.fetch_all(
            """SELECT id, not_before FROM wakeups
               WHERE status = 'scheduled'
               ORDER BY not_before LIMIT 200""",
        )
        due: list[str] = []
        for row in candidates:
            when = _parse_instant(row["not_before"])
            if when is None or when <= now:
                due.append(row["id"])   # unparseable: fire it (never otherwise due)
            if len(due) >= limit:
                break
        claimed: list[dict[str, Any]] = []
        for wid in due:
            count = await self.db.execute(
                "UPDATE wakeups SET status = 'fired' "
                "WHERE id = ? AND status = 'scheduled'",
                (wid,),
            )
            if count:
                wakeup = await self.get(wid)
                if wakeup:
                    claimed.append(wakeup)
        return claimed

    async def action_due_scheduled(self, goal_id: str, due_key: str) -> bool:
        """Dedup for the due-action sweep: has this (goal, due) already been
        woken — scheduled OR already fired? Fired rows must count, or the next
        sweep would re-create the wake forever."""
        row = await self.db.fetch_one(
            """SELECT 1 FROM wakeups
               WHERE goal_id = ? AND kind = 'action_due'
               AND json_extract(payload_json, '$.due') = ?
               AND status IN ('scheduled', 'fired') LIMIT 1""",
            (goal_id, due_key),
        )
        return row is not None

    async def list_all_scheduled(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            """SELECT id, conversation_id, goal_id, kind, not_before, recurrence,
                      tz, status, payload_json, created_at
               FROM wakeups
               WHERE status = 'scheduled'
               ORDER BY not_before
               LIMIT ?""", (limit,))
        return [dict(r) for r in rows] if rows else []

    async def recent_settled(self, *, limit: int = 30) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            """SELECT id, conversation_id, kind, not_before, recurrence, status
               FROM wakeups WHERE status != 'scheduled'
               ORDER BY not_before DESC LIMIT ?""", (limit,))
        return [dict(r) for r in rows] if rows else []

    async def scheduled_count(self) -> int:
        row = await self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM wakeups WHERE status = 'scheduled'")
        return int(row["n"]) if row else 0

    async def next_scheduled(self) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            """SELECT not_before, kind FROM wakeups WHERE status = 'scheduled'
               ORDER BY not_before LIMIT 1""")
        return dict(row) if row else None

    async def list_scheduled(self, conversation_id: str) -> list[dict[str, Any]]:
        return await self.db.fetch_all(
            "SELECT * FROM wakeups WHERE conversation_id = ? AND status = 'scheduled' "
            "ORDER BY not_before",
            (conversation_id,),
        )
