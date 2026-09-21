"""Task repository — SQL ownership for the task registry (docs/task-registry-plan.md).

A task is a durable promise: waiter, completer, result, one wake. All SQL
for the ``tasks`` table lives here. Lifecycle is CAS-once per settle; the
service layer (services/tasks.py) owns effects, wakes and sweeps.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from server.database import Database


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_task_id() -> str:
    return f"task-{secrets.token_hex(4)}"


class TaskRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(
        self, *, title: str, waiter_session: str, payload: dict[str, Any],
        expected_completer: str | None, due: str, source_goal_id: str | None,
        refs: list[str],
    ) -> dict[str, Any]:
        tid = new_task_id()
        import json
        now = _now_iso()
        await self.db.execute(
            """INSERT INTO tasks
               (id, title, waiter_session, payload_json, expected_completer,
                status, due, source_goal_id, refs_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)""",
            (tid, title, waiter_session, json.dumps(payload, ensure_ascii=False),
             expected_completer, due, source_goal_id,
             json.dumps(refs or []), now, now))
        return (await self.get(tid))  # type: ignore[return-value]

    async def get(self, task_id: str) -> dict[str, Any] | None:
        return await self.db.fetch_one(
            "SELECT * FROM tasks WHERE id = ?", (task_id,))

    async def get_by_short_id(self, ref: str) -> dict[str, Any] | None:
        """Resolve 'task-a3f2' or a bare 'a3f2' suffix to its unique row —
        the chat-facing short-id surface (Mike's Q5)."""
        ref = ref.strip().removeprefix("task-")
        if not ref:
            return None
        return await self.db.fetch_one(
            "SELECT * FROM tasks WHERE id LIKE ? "
            "AND (SELECT COUNT(*) FROM tasks t2 WHERE t2.id LIKE ?) = 1",
            (f"task-{ref}%", f"task-{ref}%"))

    async def settle(
        self, task_id: str, *, to_status: str, result: str | None = None,
        error: str | None = None, completed_by: str,
    ) -> dict[str, Any] | None:
        """CAS-once settle: pending → completed|failed|cancelled. Returns the
        settled row on win, None when already settled (idempotent no-op for
        replays, retries and double-confirmations)."""
        now = _now_iso()
        count = await self.db.execute(
            "UPDATE tasks SET status = ?, result = ?, error = ?, "
            "completed_by = ?, completed_at = ?, updated_at = ? "
            "WHERE id = ? AND status = 'pending'",
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
            "UPDATE tasks SET delivered_at = ?, updated_at = ? WHERE id = ?",
            (_now_iso(), _now_iso(), task_id))

    async def list_for_waiter(
        self, waiter_session: str, *, status: str = "pending", limit: int = 20,
    ) -> list[dict[str, Any]]:
        return await self.db.fetch_all(
            "SELECT * FROM tasks WHERE waiter_session = ? AND status = ? "
            "ORDER BY due LIMIT ?", (waiter_session, status, limit))

    async def list_for_completer(
        self, session_key: str, *, limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Pending tasks this conversation is expected to complete — the
        completer-side visibility block's backing query (plan D6)."""
        return await self.db.fetch_all(
            "SELECT * FROM tasks WHERE expected_completer = ? "
            "AND status = 'pending' ORDER BY due LIMIT ?",
            (session_key, limit))

    async def pending_past_due(self, *, now_iso: str, limit: int = 50) -> list[dict[str, Any]]:
        return await self.db.fetch_all(
            "SELECT * FROM tasks WHERE status = 'pending' AND due < ? "
            "ORDER BY due LIMIT ?", (now_iso, limit))

    async def pending_all(self, *, limit: int = 200) -> list[dict[str, Any]]:
        return await self.db.fetch_all(
            "SELECT * FROM tasks WHERE status = 'pending' "
            "ORDER BY created_at LIMIT ?", (limit,))

    # -- goal loop (docs/goal-execution-plan.md): branches per goal --------

    async def counts_for_goal(
        self, goal_id: str, room_session: str, *, since_iso: str | None = None,
    ) -> dict[str, int]:
        """Loop delta metrics: open now; spawned/settled since a turn start.
        Scope = registered by the goal (source_goal_id) or awaited by its
        room — both shapes are the goal's branches."""
        scope = "(source_goal_id = ? OR waiter_session = ?)"
        if since_iso is not None:
            open_now = await self.db.fetch_one(
                f"SELECT COUNT(*) AS n FROM tasks WHERE {scope} "
                "AND status = 'pending'", (goal_id, room_session))
            spawned = await self.db.fetch_one(
                f"SELECT COUNT(*) AS n FROM tasks WHERE {scope} "
                "AND created_at >= ?", (goal_id, room_session, since_iso))
            settled = await self.db.fetch_one(
                f"SELECT COUNT(*) AS n FROM tasks WHERE {scope} "
                "AND status != 'pending' AND completed_at >= ?",
                (goal_id, room_session, since_iso))
            return {"open": open_now["n"], "spawned": spawned["n"],
                    "settled": settled["n"]}
        open_now = await self.db.fetch_one(
            f"SELECT COUNT(*) AS n FROM tasks WHERE {scope} "
            "AND status = 'pending'", (goal_id, room_session))
        return {"open": open_now["n"], "spawned": 0, "settled": 0}

    async def open_counts_by_goal(self, goal_ids: list[str]) -> dict[str, int]:
        if not goal_ids:
            return {}
        marks = ",".join("?" * len(goal_ids))
        rows = await self.db.fetch_all(
            f"SELECT source_goal_id AS gid, COUNT(*) AS n FROM tasks "
            f"WHERE status = 'pending' AND source_goal_id IN ({marks}) "
            f"GROUP BY source_goal_id", tuple(goal_ids))
        return {r["gid"]: r["n"] for r in rows}

    async def list_for_goal(
        self, goal_id: str, room_session: str, *, limit: int = 60,
    ) -> list[dict[str, Any]]:
        return await self.db.fetch_all(
            "SELECT * FROM tasks WHERE source_goal_id = ? "
            "OR waiter_session = ? ORDER BY created_at DESC LIMIT ?",
            (goal_id, room_session, limit))
