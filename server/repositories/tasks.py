"""Task repository — SQL ownership for task promises (docs/task-registry-plan.md).

A task is a durable promise: waiter, completer, result, one wake. Since
commitments plan Phase 2 (2026-10-05) a task is stored as a promise-profile
goal (``goals`` row, kind 'promise'); this repository keeps the task-shaped
interface and field names on top of it, so the service layer is unchanged.
The old ``tasks`` table is a frozen archive.

Promises keep the task status vocabulary (pending → completed | failed |
cancelled); goal sweepers only scan status 'active', so they never touch a
promise. Lifecycle is CAS-once per settle; services/tasks.py owns effects,
wakes and sweeps.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any

from server.database import Database

# goals columns under the task field names every caller uses.
_SELECT = (
    "SELECT id, objective AS title, origin_conversation_id AS waiter_session, "
    "COALESCE(payload_json, '{}') AS payload_json, "
    "completer AS expected_completer, status, result, error, completed_by, "
    "completed_at, delivered_at, deadline AS due, source_goal_id, "
    "COALESCE(refs_json, '[]') AS refs_json, created_at, updated_at "
    "FROM goals")
_PROMISE = "kind = 'promise'"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_task_id() -> str:
    return f"prm-{secrets.token_hex(4)}"


class TaskRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(
        self, *, title: str, waiter_session: str, payload: dict[str, Any],
        expected_completer: str | None, due: str, source_goal_id: str | None,
        refs: list[str],
    ) -> dict[str, Any]:
        tid = new_task_id()
        now = _now_iso()
        await self.db.execute(
            """INSERT INTO goals
               (id, conversation_id, origin_conversation_id, kind, profile,
                objective, status, deadline, payload_json, completer,
                source_goal_id, refs_json, version, created_at, updated_at)
               VALUES (?, ?, ?, 'promise', 'promise', ?, 'pending', ?, ?, ?,
                       ?, ?, 1, ?, ?)""",
            (tid, waiter_session, waiter_session, title, due,
             json.dumps(payload, ensure_ascii=False), expected_completer,
             source_goal_id, json.dumps(refs or []), now, now))
        return (await self.get(tid))  # type: ignore[return-value]

    async def get(self, task_id: str) -> dict[str, Any] | None:
        return await self.db.fetch_one(
            f"{_SELECT} WHERE id = ? AND {_PROMISE}", (task_id,))

    async def get_by_short_id(self, ref: str) -> dict[str, Any] | None:
        """Resolve 'prm-a3f2' or a bare 'a3f2' suffix to its unique row —
        the chat-facing short-id surface (Mike's Q5). Promises minted before
        2026-10-06 carry the old 'task-' prefix; both resolve."""
        ref = ref.strip().removeprefix("prm-").removeprefix("task-")
        if not ref:
            return None
        pattern = f"%-{ref}%"
        return await self.db.fetch_one(
            f"{_SELECT} WHERE {_PROMISE} AND id LIKE ? "
            f"AND (SELECT COUNT(*) FROM goals g2 WHERE g2.kind = 'promise' "
            f"AND g2.id LIKE ?) = 1",
            (pattern, pattern))

    async def settle(
        self, task_id: str, *, to_status: str, result: str | None = None,
        error: str | None = None, completed_by: str,
    ) -> dict[str, Any] | None:
        """CAS-once settle: pending → completed|failed|cancelled. Returns the
        settled row on win, None when already settled (idempotent no-op for
        replays, retries and double-confirmations)."""
        now = _now_iso()
        count = await self.db.execute(
            "UPDATE goals SET status = ?, result = ?, error = ?, "
            "completed_by = ?, completed_at = ?, updated_at = ? "
            f"WHERE id = ? AND {_PROMISE} AND status = 'pending'",
            (to_status, result, error, completed_by, now, now, task_id))
        if not count:
            return None
        return await self.get(task_id)

    async def mark_delivered(self, task_id: str) -> None:
        """Stamp the exactly-once wake delivery (called only AFTER a
        successful waiter wake — a failed wake stays undelivered so the
        effect retries; the crash window the other way costs at most one
        duplicate wake, never a lost one)."""
        await self.db.execute(
            f"UPDATE goals SET delivered_at = ?, updated_at = ? "
            f"WHERE id = ? AND {_PROMISE}",
            (_now_iso(), _now_iso(), task_id))

    async def list_for_waiter(
        self, waiter_session: str, *, status: str = "pending", limit: int = 20,
    ) -> list[dict[str, Any]]:
        return await self.db.fetch_all(
            f"{_SELECT} WHERE {_PROMISE} AND origin_conversation_id = ? "
            "AND status = ? ORDER BY deadline LIMIT ?",
            (waiter_session, status, limit))

    async def list_for_completer(
        self, session_key: str, *, limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Pending tasks this conversation is expected to complete — the
        completer-side visibility block's backing query (plan D6)."""
        return await self.db.fetch_all(
            f"{_SELECT} WHERE {_PROMISE} AND completer = ? "
            "AND status = 'pending' ORDER BY deadline LIMIT ?",
            (session_key, limit))

    async def pending_past_due(self, *, now_iso: str, limit: int = 50) -> list[dict[str, Any]]:
        return await self.db.fetch_all(
            f"{_SELECT} WHERE {_PROMISE} AND status = 'pending' AND deadline < ? "
            "ORDER BY deadline LIMIT ?", (now_iso, limit))

    async def pending_all(self, *, limit: int = 200) -> list[dict[str, Any]]:
        return await self.db.fetch_all(
            f"{_SELECT} WHERE {_PROMISE} AND status = 'pending' "
            "ORDER BY created_at LIMIT ?", (limit,))

    # -- goal loop (docs/goal-execution-plan.md): branches per goal --------

    async def counts_for_goal(
        self, goal_id: str, room_session: str, *, since_iso: str | None = None,
    ) -> dict[str, int]:
        """Loop delta metrics: open now; spawned/settled since a turn start.
        Scope = registered by the goal (source_goal_id), plus tasks awaited
        on its ROOM session when the goal has one. Non-room goals work
        inside a group/DM that awaits plenty of unrelated tasks — the
        waiter clause is room-shaped only (the 2026-09-25 mug-delivery
        leak onto the figurine-set goal's drill-down)."""
        base, params = self._goal_scope(goal_id, room_session)
        count = f"SELECT COUNT(*) AS n FROM goals WHERE {_PROMISE} AND {base}"
        async def _n(sql: str, args: list) -> int:
            row = await self.db.fetch_one(sql, tuple(args))
            return int(row["n"]) if row else 0

        open_now = await _n(f"{count} AND status = 'pending'", params)
        if since_iso is None:
            return {"open": open_now, "spawned": 0, "settled": 0}
        return {
            "open": open_now,
            "spawned": await _n(f"{count} AND created_at >= ?", params + [since_iso]),
            "settled": await _n(
                f"{count} AND status != 'pending' AND completed_at >= ?",
                params + [since_iso]),
        }

    def _goal_scope(self, goal_id: str, room_session: str) -> tuple[str, list]:
        """(WHERE-fragment-without-WHERE, params) for a goal's branch scope."""
        if room_session.startswith("agent:goal-"):
            return ("(source_goal_id = ? OR origin_conversation_id = ?)",
                    [goal_id, room_session])
        return ("source_goal_id = ?", [goal_id])

    async def open_counts_by_goal(self, goal_ids: list[str]) -> dict[str, int]:
        if not goal_ids:
            return {}
        marks = ",".join("?" * len(goal_ids))
        rows = await self.db.fetch_all(
            f"SELECT source_goal_id AS gid, COUNT(*) AS n FROM goals "
            f"WHERE {_PROMISE} AND status = 'pending' "
            f"AND source_goal_id IN ({marks}) GROUP BY source_goal_id",
            tuple(goal_ids))
        return {r["gid"]: r["n"] for r in rows}

    async def list_for_goal(
        self, goal_id: str, room_session: str, *, limit: int = 60,
    ) -> list[dict[str, Any]]:
        base, params = self._goal_scope(goal_id, room_session)
        return await self.db.fetch_all(
            f"{_SELECT} WHERE {_PROMISE} AND {base} "
            "ORDER BY created_at DESC LIMIT ?", tuple(params + [limit]))

    async def repoint_waiter(self, task_id: str, waiter_session: str) -> int:
        """CAS-ish move of a pending task to a new waiter (goal recreate
        carry-over; the backstop repoint lives in wakeups repo)."""
        return await self.db.execute(
            "UPDATE goals SET origin_conversation_id = ?, conversation_id = ?, "
            f"updated_at = ? WHERE id = ? AND {_PROMISE} AND status = 'pending'",
            (waiter_session, waiter_session, _now_iso(), task_id))

    async def delete_for_waiter(self, waiter_session: str) -> int:
        """Eval-fixture cleanup (goal_behavior G3 seeds real task rows via
        task_register); SQL lives here per the ownership rule."""
        return await self.db.execute(
            f"DELETE FROM goals WHERE {_PROMISE} AND origin_conversation_id = ?",
            (waiter_session,))

    async def latest_for_waiter(self, waiter_session: str) -> dict | None:
        """Newest task row for a waiter (eval context flag)."""
        return await self.db.fetch_one(
            f"{_SELECT} WHERE {_PROMISE} AND origin_conversation_id = ? "
            "ORDER BY created_at DESC LIMIT 1", (waiter_session,))
