"""Goal repository — durable intent with CAS transitions (Bob3 Phase V).

A goal is intent a conversation holds toward an outcome. Status changes CAS
on the current status; revisions CAS on the version number, so a stale
strategy can never overwrite a newer one. Every status change is recorded in
``goal_transitions``.

``conversation_id`` / ``origin_conversation_id`` carry session_keys until
Phase VI introduces conversations proper.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from bob_server.database import Database

TERMINAL_STATUSES = ("completed", "failed", "cancelled")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class GoalRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(
        self,
        *,
        conversation_id: str,
        objective: str,
        origin_conversation_id: str | None = None,
        kind: str = "task",
        strategy_json: str | None = None,
        deadline: str | None = None,
        external_ref: str | None = None,
        goal_id: str | None = None,
    ) -> dict[str, Any]:
        gid = goal_id or str(uuid.uuid4())
        now = _now_iso()
        await self.db.execute(
            """INSERT INTO goals
               (id, conversation_id, origin_conversation_id, kind, objective,
                strategy_json, deadline, external_ref, status, version,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, ?)""",
            (gid, conversation_id, origin_conversation_id, kind, objective,
             strategy_json, deadline, external_ref, now, now),
        )
        return (await self.get(gid))  # type: ignore[return-value]

    async def get(self, goal_id: str) -> dict[str, Any] | None:
        return await self.db.fetch_one("SELECT * FROM goals WHERE id = ?", (goal_id,))

    async def get_by_external_ref(self, external_ref: str) -> dict[str, Any] | None:
        return await self.db.fetch_one(
            "SELECT * FROM goals WHERE external_ref = ? ORDER BY created_at DESC LIMIT 1",
            (external_ref,),
        )

    async def list_active(
        self,
        *,
        conversation_id: str | None = None,
        origin_conversation_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses = ["status = 'active'"]
        params: list[Any] = []
        if conversation_id is not None:
            clauses.append("conversation_id = ?")
            params.append(conversation_id)
        if origin_conversation_id is not None:
            clauses.append("origin_conversation_id = ?")
            params.append(origin_conversation_id)
        params.append(limit)
        return await self.db.fetch_all(
            f"SELECT * FROM goals WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        )

    async def revise(
        self,
        goal_id: str,
        *,
        expected_version: int,
        objective: str | None = None,
        strategy_json: str | None = None,
        progress: str | None = None,
        deadline: str | None = None,
    ) -> bool:
        """CAS revision: applies only if the version matches (stale-strategy
        protection). Bumps version on success."""
        sets = ["version = version + 1", "updated_at = ?"]
        params: list[Any] = [_now_iso()]
        for col, val in (("objective", objective), ("strategy_json", strategy_json),
                         ("progress", progress), ("deadline", deadline)):
            if val is not None:
                sets.append(f"{col} = ?")
                params.append(val)
        params.extend([goal_id, expected_version])
        count = await self.db.execute(
            f"UPDATE goals SET {', '.join(sets)} "
            "WHERE id = ? AND version = ? AND status = 'active'",
            tuple(params),
        )
        return bool(count)

    async def transition(
        self,
        goal_id: str,
        *,
        to_status: str,
        from_status: str = "active",
        result: str | None = None,
        note: str | None = None,
    ) -> bool:
        """CAS status transition; records a goal_transitions row on success.

        Returns False when the goal is not in ``from_status`` (e.g. the race
        of simultaneous completion, or cancel-vs-complete) — the loser of the
        race must treat the goal as already settled.
        """
        now = _now_iso()
        async with self.db.transaction() as txn:
            sets = "status = ?, version = version + 1, updated_at = ?"
            params: list[Any] = [to_status, now]
            if result is not None:
                sets += ", result = ?"
                params.append(result)
            params.extend([goal_id, from_status])
            count = await txn.execute(
                f"UPDATE goals SET {sets} WHERE id = ? AND status = ?",
                tuple(params),
            )
            if not count:
                return False
            row = await txn.fetch_one(
                "SELECT version FROM goals WHERE id = ?", (goal_id,))
            await txn.execute(
                """INSERT INTO goal_transitions
                   (id, goal_id, from_status, to_status, version, note, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), goal_id, from_status, to_status,
                 row["version"] if row else 0, note, now),
            )
            return True

    async def complete(self, goal_id: str, *, result: str, note: str | None = None) -> bool:
        return await self.transition(goal_id, to_status="completed", result=result, note=note)

    async def fail(self, goal_id: str, *, error: str, note: str | None = None) -> bool:
        return await self.transition(goal_id, to_status="failed", result=error, note=note)

    async def cancel(self, goal_id: str, *, note: str | None = None) -> bool:
        return await self.transition(goal_id, to_status="cancelled", note=note)

    async def transitions(self, goal_id: str) -> list[dict[str, Any]]:
        return await self.db.fetch_all(
            "SELECT * FROM goal_transitions WHERE goal_id = ? ORDER BY created_at",
            (goal_id,),
        )
