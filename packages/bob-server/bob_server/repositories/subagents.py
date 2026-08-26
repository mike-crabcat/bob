"""Subagent repository — owns all subagents SQL.

Lifecycle: created → running → waiting_for_parent → completed/failed.
Voice subagents ('openai_voice') skip waiting_for_parent and are completed
directly by the voice dispatch when the call ends.
"""

from __future__ import annotations

from typing import Any


class SubagentRepository:
    def __init__(self, db: Any):
        self.db = db

    async def insert(
        self,
        *,
        subagent_id: str,
        parent_session_key: str,
        session_key: str,
        task: str,
        agent_type: str,
        persona: int,
        model: str | None,
        contact_id: str | None,
        modality: str | None,
        now_iso: str,
    ) -> None:
        await self.db.execute(
            """INSERT INTO subagents
               (id, parent_session_key, session_key, task, status, agent_type, persona, model,
                contact_id, modality, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'created', ?, ?, ?, ?, ?, ?, ?)""",
            (subagent_id, parent_session_key, session_key, task, agent_type,
             persona, model, contact_id, modality, now_iso, now_iso))

    # -------------------------------------------------------------- reads

    async def get(self, subagent_id: str) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            "SELECT * FROM subagents WHERE id = ?", (subagent_id,))
        return dict(row) if row else None

    async def session_key_of(self, subagent_id: str) -> str | None:
        row = await self.db.fetch_one(
            "SELECT session_key FROM subagents WHERE id = ?", (subagent_id,))
        return row["session_key"] if row else None

    async def status_of(self, subagent_id: str) -> str | None:
        row = await self.db.fetch_one(
            "SELECT status FROM subagents WHERE id = ?", (subagent_id,))
        return row["status"] if row else None

    async def list_for_parent(
        self, parent_session_key: str, *, status: str = "", limit: int = 20,
    ) -> list[dict[str, Any]]:
        query = ("SELECT id, status, substr(task, 1, 100) as task_preview, cost_usd, created_at "
                 "FROM subagents WHERE parent_session_key = ?")
        params: list[Any] = [parent_session_key]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = await self.db.fetch_all(query, tuple(params))
        return [dict(r) for r in rows] if rows else []

    async def recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM subagents ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows] if rows else []

    # ---------------------------------------------------------- lifecycle

    async def set_status(
        self, subagent_id: str, status: str, now_iso: str, *, error: str | None = None,
    ) -> None:
        if error:
            await self.db.execute(
                "UPDATE subagents SET status = ?, error_message = ?, updated_at = ? WHERE id = ?",
                (status, error, now_iso, subagent_id))
        else:
            await self.db.execute(
                "UPDATE subagents SET status = ?, updated_at = ? WHERE id = ?",
                (status, now_iso, subagent_id))

    async def store_result(
        self,
        subagent_id: str,
        *,
        result: str,
        claude_session_id: str,
        cost_usd: float,
        now_iso: str,
    ) -> None:
        """Result arrived: park the subagent waiting for its parent."""
        await self.db.execute(
            """UPDATE subagents
               SET status = 'waiting_for_parent', result = ?,
                   claude_session_id = ?, cost_usd = ?, updated_at = ?
               WHERE id = ?""",
            (result, claude_session_id, cost_usd, now_iso, subagent_id))

    async def complete_voice(self, subagent_id: str, result_text: str) -> None:
        """Voice calls complete directly; result is the call summary."""
        await self.db.execute(
            """UPDATE subagents
               SET status = 'completed', result = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (result_text, subagent_id))

    async def fail_stale(self, now_iso: str) -> int:
        """Fail created/running subagents on restart (their asyncio tasks
        died with the process). Voice subagents are excluded from that
        immediate sweep — they legitimately stay 'running' while the phone
        rings — but age horizons reap what the sweep can never catch: a
        voice call that never reported an outcome (leaked row), and
        finished work whose parent is long gone."""
        total = 0
        # Restart sweep: non-voice tasks died with the process.
        total += await self.db.execute(
            "UPDATE subagents SET status = 'failed', error_message = 'Server restarted', updated_at = ? "
            "WHERE status IN ('created', 'running') AND agent_type != 'openai_voice'",
            (now_iso,))
        # A voice call still 'running' after a day reported no outcome.
        total += await self.db.execute(
            "UPDATE subagents SET status = 'failed', "
            "error_message = 'Reaped: voice call never reported an outcome', updated_at = ? "
            "WHERE status = 'running' AND agent_type = 'openai_voice' "
            "AND updated_at < datetime('now', '-24 hours')",
            (now_iso,))
        # waiting_for_parent older than a week: the result relay is long
        # abandoned; finish the bookkeeping (result text is preserved).
        total += await self.db.execute(
            "UPDATE subagents SET status = 'completed', updated_at = ? "
            "WHERE status = 'waiting_for_parent' "
            "AND updated_at < datetime('now', '-7 days')",
            (now_iso,))
        return total
