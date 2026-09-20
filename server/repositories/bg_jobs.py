"""bg_jobs repository — owns all bg_jobs SQL.

Lifecycle: running → terminal (exited|failed|timeout|killed|orphaned), or
→ replaced when the name is re-started. Wake jobs (wake_on_exit=1) carry a
delivery ledger ('' → pending → delivered) drained by the exit watcher in
process_tools; the watcher is the only writer of terminal states.
"""

from __future__ import annotations

from typing import Any

TERMINAL_STATUSES = ("exited", "failed", "timeout", "killed", "orphaned", "replaced")


class BgJobsRepository:
    def __init__(self, db: Any):
        self.db = db

    # -------------------------------------------------------------- writes

    async def create(
        self,
        *,
        name: str,
        command: str,
        unit: str | None,
        mechanism: str,
        pid: int | None,
        pid_start_time: str | None,
        description: str,
        source: str,
        wake_on_exit: bool,
        parent_session_key: str,
        log: str,
        now_iso: str,
    ) -> int:
        await self.db.execute(
            """INSERT INTO bg_jobs
               (name, unit, mechanism, pid, pid_start_time, command, description,
                source, status, wake_on_exit, parent_session_key, delivery, log,
                started_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, '', ?, ?, ?, ?)""",
            (name, unit, mechanism, pid, pid_start_time, command, description,
             source, int(wake_on_exit), parent_session_key, log,
             now_iso, now_iso, now_iso))
        row = await self.db.fetch_one(
            "SELECT id FROM bg_jobs WHERE name = ? AND status = 'running'", (name,))
        return int(row["id"]) if row else 0

    async def mark_terminal(
        self, job_id: int, *, status: str, exit_code: int | None,
        systemd_result: str | None, now_iso: str,
    ) -> None:
        await self.db.execute(
            """UPDATE bg_jobs
               SET status = ?, exit_code = ?, systemd_result = ?,
                   ended_at = ?, updated_at = ?
               WHERE id = ?""",
            (status, exit_code, systemd_result, now_iso, now_iso, job_id))

    async def mark_replaced(self, job_id: int, now_iso: str) -> None:
        await self.db.execute(
            """UPDATE bg_jobs
               SET status = 'replaced', ended_at = ?, updated_at = ?
               WHERE id = ? AND status = 'running'""",
            (now_iso, now_iso, job_id))

    async def set_delivery(self, job_id: int, state: str, now_iso: str) -> None:
        await self.db.execute(
            "UPDATE bg_jobs SET delivery = ?, updated_at = ? WHERE id = ?",
            (state, now_iso, job_id))

    # -------------------------------------------------------------- reads

    async def get(self, job_id: int) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            "SELECT * FROM bg_jobs WHERE id = ?", (job_id,))
        return dict(row) if row else None

    async def running_rows(self) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM bg_jobs WHERE status = 'running' ORDER BY started_at")
        return [dict(r) for r in rows] if rows else []

    async def get_running_by_name(self, name: str) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            "SELECT * FROM bg_jobs WHERE name = ? AND status = 'running'", (name,))
        return dict(row) if row else None

    async def pending_deliveries(self) -> list[dict[str, Any]]:
        """Wake jobs whose exit was recorded but whose wake hasn't landed
        (watcher retries; boot pass redelivers)."""
        rows = await self.db.fetch_all(
            """SELECT * FROM bg_jobs
               WHERE delivery = 'pending' AND wake_on_exit = 1
               ORDER BY ended_at""")
        return [dict(r) for r in rows] if rows else []

    async def list(
        self, *, status: str | None = None, limit: int = 50,
    ) -> list[dict[str, Any]]:
        if status:
            rows = await self.db.fetch_all(
                "SELECT * FROM bg_jobs WHERE status = ? ORDER BY started_at DESC LIMIT ?",
                (status, limit))
        else:
            rows = await self.db.fetch_all(
                "SELECT * FROM bg_jobs ORDER BY started_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows] if rows else []

    async def count_running(self) -> int:
        row = await self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM bg_jobs WHERE status = 'running'")
        return int(row["n"]) if row else 0
