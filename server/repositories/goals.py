"""Goal repository — durable intent with CAS transitions (Bob3 Phase V).

A goal is intent a conversation holds toward an outcome. Status changes CAS
on the current status; revisions CAS on the version number, so a stale
strategy can never overwrite a newer one. Every status change is recorded in
``goal_transitions``.

``conversation_id`` / ``origin_conversation_id`` carry session_keys until
Phase VI introduces conversations proper. The Bob Events hierarchy
(migration 459) adds ``parent_goal_id`` and the ``goal_conversations`` holder
set — those rows hold CANONICAL conversation ids (``resolve_cid()``), never
raw session_keys.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from server.database import Database

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
        parent_goal_id: str | None = None,
        goal_id: str | None = None,
    ) -> dict[str, Any]:
        gid = goal_id or str(uuid.uuid4())
        now = _now_iso()
        await self.db.execute(
            """INSERT INTO goals
               (id, conversation_id, origin_conversation_id, kind, objective,
                strategy_json, deadline, external_ref, parent_goal_id, status,
                version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, ?)""",
            (gid, conversation_id, origin_conversation_id, kind, objective,
             strategy_json, deadline, external_ref, parent_goal_id, now, now),
        )
        return (await self.get(gid))  # type: ignore[return-value]

    async def children_of(
        self, parent_goal_id: str, *, status: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = ("SELECT * FROM goals WHERE parent_goal_id = ?"
               + (" AND status = ?" if status else "")
               + " ORDER BY created_at")
        params: tuple[Any, ...] = ((parent_goal_id, status) if status
                                   else (parent_goal_id,))
        return await self.db.fetch_all(sql, params)

    async def root_of(self, goal_id: str) -> dict[str, Any] | None:
        """Walk the parent chain to the root goal (bounded depth; cycles are
        structurally impossible but a hard cap keeps a corrupt row from
        looping forever)."""
        current = await self.get(goal_id)
        seen: set[str] = set()
        while current is not None and current.get("parent_goal_id"):
            pid = current["parent_goal_id"]
            if pid in seen:
                break
            seen.add(pid)
            current = await self.get(pid)
        return current

    async def add_holder(self, goal_id: str, conversation_id: str,
                         *, role: str = "holder") -> None:
        await self.db.execute(
            """INSERT OR REPLACE INTO goal_conversations
               (goal_id, conversation_id, role, created_at)
               VALUES (?, ?, ?, ?)""",
            (goal_id, conversation_id, role, _now_iso()),
        )

    async def holders_of(self, goal_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT goal_id, conversation_id, role, created_at "
            "FROM goal_conversations WHERE goal_id = ? ORDER BY created_at",
            (goal_id,))
        return [dict(r) for r in rows] if rows else []

    async def goals_held_by(
        self, conversation_id: str, *, active_only: bool = True, limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Goals a conversation holds (any role), newest activity first —
        the prompt-injection query (plan §1.4)."""
        where = "1=1" if not active_only else "g.status = 'active'"
        rows = await self.db.fetch_all(
            f"""SELECT g.* FROM goals g
                JOIN goal_conversations gc ON gc.goal_id = g.id
                WHERE gc.conversation_id = ? AND {where}
                ORDER BY g.updated_at DESC LIMIT ?""",
            (conversation_id, limit))
        return [dict(r) for r in rows] if rows else []

    async def active_goal_ids_referencing_entities(
        self, entity_ids: list[str],
    ) -> list[str]:
        """Active goals whose strategy refs.entities names any of the given
        entities — the claim router's strong (ref) match (Bob Events §2.3)."""
        if not entity_ids:
            return []
        marks = ",".join("?" for _ in entity_ids)
        rows = await self.db.fetch_all(
            f"""SELECT DISTINCT id FROM goals
                WHERE status = 'active'
                  AND json_extract(strategy_json, '$.refs.entities') IS NOT NULL
                  AND EXISTS (SELECT 1 FROM json_each(
                      json_extract(strategy_json, '$.refs.entities')) je
                      WHERE je.value IN ({marks}))""",
            tuple(entity_ids))
        return [r["id"] for r in rows or []]

    async def active_goal_ids_held_by_conversations(
        self, conversation_ids: list[str], *,
        exclude_conversation_id: str | None = None,
    ) -> list[str]:
        """Active goals held by any of the given conversations (holder-based
        claim-router matching; the excluded conversation implements echo
        suppression — a goal held only by the originating conversation is not
        a candidate through that link)."""
        if not conversation_ids:
            return []
        marks = ",".join("?" for _ in conversation_ids)
        params: list[Any] = list(conversation_ids)
        exclude = ""
        if exclude_conversation_id is not None:
            exclude = "AND gc.conversation_id != ?"
            params.append(exclude_conversation_id)
        rows = await self.db.fetch_all(
            f"""SELECT DISTINCT g.id FROM goals g
                JOIN goal_conversations gc ON gc.goal_id = g.id
                WHERE g.status = 'active' AND gc.conversation_id IN ({marks})
                {exclude}""",
            tuple(params))
        return [r["id"] for r in rows or []]

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

    async def list_recent(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            """SELECT id, conversation_id, origin_conversation_id, kind, objective,
                      progress, result, status, deadline, created_at, updated_at
               FROM goals
               ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, updated_at DESC
               LIMIT ?""", (limit,))
        return [dict(r) for r in rows] if rows else []

    async def transitions_for(self, goal_ids: list[str]) -> list[dict[str, Any]]:
        if not goal_ids:
            return []
        marks = ",".join("?" * len(goal_ids))
        rows = await self.db.fetch_all(
            f"""SELECT goal_id, from_status, to_status, note, created_at
                FROM goal_transitions WHERE goal_id IN ({marks})
                ORDER BY created_at""", tuple(goal_ids))
        return [dict(r) for r in rows] if rows else []

    async def recent_transitions(
        self, conversation_id: str, *, limit: int = 50,
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            """SELECT gt.created_at, gt.from_status, gt.to_status, gt.note,
                      g.id AS goal_id, g.objective
               FROM goal_transitions gt JOIN goals g ON g.id = gt.goal_id
               WHERE g.conversation_id = ?
               ORDER BY gt.created_at DESC LIMIT ?""", (conversation_id, limit))
        return [dict(r) for r in rows] if rows else []

    async def stale_active(self, *, older_than: str, limit: int = 10) -> list[dict[str, Any]]:
        """Active goals not touched since ``older_than``, stallest first —
        the progress-review loop's scan (plan §4.1). Parsed in Python:
        updated_at writers emit full microsecond ISO."""
        rows = await self.db.fetch_all(
            "SELECT * FROM goals WHERE status = 'active'")
        cutoff = older_than[:19]
        stale = [dict(r) for r in rows or []
                 if (r["updated_at"] or "")[:19] < cutoff]
        stale.sort(key=lambda r: r["updated_at"] or "")
        return stale[:limit]

    async def children_map(self, goal_ids: list[str]) -> dict[str, list[str]]:
        """parent_goal_id → [child ids], for the goal-tree dashboard view."""
        if not goal_ids:
            return {}
        marks = ",".join("?" for _ in goal_ids)
        rows = await self.db.fetch_all(
            f"SELECT parent_goal_id, id FROM goals "
            f"WHERE parent_goal_id IN ({marks}) ORDER BY created_at",
            tuple(goal_ids))
        out: dict[str, list[str]] = {}
        for r in rows or []:
            out.setdefault(r["parent_goal_id"], []).append(r["id"])
        return out

    async def active_count(self) -> int:
        row = await self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM goals WHERE status = 'active'")
        return int(row["n"]) if row else 0

    async def overdue(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Active goals whose deadline passed. Compared in Python: deadline
        writers emit full microsecond ISO with offset, so a TEXT compare
        against a second-truncated strftime 'now' mismatches at the edges."""
        rows = await self.db.fetch_all(
            """SELECT id, objective, kind, deadline, conversation_id FROM goals
               WHERE status = 'active' AND deadline IS NOT NULL""")
        now = datetime.now(timezone.utc)
        due: list[dict[str, Any]] = []
        for row in rows or []:
            try:
                dl = datetime.fromisoformat(str(row["deadline"]).replace("Z", "+00:00"))
            except ValueError:
                continue
            if dl.tzinfo is None:
                dl = dl.replace(tzinfo=timezone.utc)
            if dl <= now:
                due.append(dict(row))
        due.sort(key=lambda r: r["deadline"])
        return due[:limit]

    async def active_outreach(self, conversation_id: str) -> dict[str, Any] | None:
        """Latest active outreach goal held by a conversation."""
        row = await self.db.fetch_one(
            "SELECT id, objective, origin_conversation_id, strategy_json FROM goals "
            "WHERE conversation_id = ? AND kind = 'outreach' AND status = 'active' "
            "ORDER BY created_at DESC LIMIT 1",
            (conversation_id,))
        return dict(row) if row else None

    async def transitions(self, goal_id: str) -> list[dict[str, Any]]:
        return await self.db.fetch_all(
            "SELECT * FROM goal_transitions WHERE goal_id = ? ORDER BY created_at",
            (goal_id,),
        )
