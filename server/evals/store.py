"""Eval result store — eval_runs + eval_case_results (docs/evals.md).

The store is its own SQLite file (~/data/evals.db, BOB_EVAL_DB), built
from the standard migrations; one eval_case_results row is one TRIAL
(case × repeat_index).
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from server.database import Database

_RUN_COLS = ("model", "provider_pin", "effort", "judge_model", "git_sha",
             "git_dirty", "persona_hash", "repeats", "tier", "case_filter",
             "notes")


class EvalStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def start_run(self, *, category: str | None, meta: dict[str, Any]) -> str:
        run_id = str(uuid4())
        cols = [c for c in _RUN_COLS if c in meta]
        await self.db.execute(
            "INSERT INTO eval_runs (id, category, status"
            + "".join(f", {c}" for c in cols)
            + ") VALUES (?, ?, 'running'" + ", ?" * len(cols) + ")",
            (run_id, category, *[meta[c] for c in cols]))
        return run_id

    async def complete_run(self, run_id: str, *, total: int, passed: int,
                           tokens: int | None, cost: float | None,
                           status: str = "completed") -> None:
        await self.db.execute(
            "UPDATE eval_runs SET completed_at = datetime('now'), "
            "total_cases = ?, passed_cases = ?, failed_cases = ?, "
            "overall_pass_rate = ?, total_tokens = ?, total_cost_usd = ?, "
            "status = ? WHERE id = ?",
            (total, passed, total - passed, (passed / total) if total else 0.0,
             tokens, cost, status, run_id))

    async def record_trial(self, run_id: str, t: dict[str, Any]) -> None:
        def _j(v: Any) -> str | None:
            return None if v is None else json.dumps(v, default=str)

        await self.db.execute(
            """INSERT INTO eval_case_results
               (id, run_id, case_id, category, passed, llm_response,
                llm_latency_seconds, judge_score, judge_reasoning,
                structural_results_json, input_messages_json, error_message,
                repeat_index, failure_class, prompt_tokens, completion_tokens,
                cost_usd, llm_calls, judge_items_json, tool_calls_json,
                deliveries_json, flags_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?)""",
            (str(uuid4()), run_id, t["case_id"], t["category"],
             1 if t["passed"] else 0, (t.get("response") or "")[:50000],
             t.get("latency"), t.get("judge_score"), t.get("judge_reasoning"),
             _j(t.get("checks")), _j(t.get("transcript")), t.get("error"),
             t.get("repeat_index", 0), t.get("failure_class"),
             t.get("prompt_tokens"), t.get("completion_tokens"),
             t.get("cost_usd"), t.get("llm_calls"), _j(t.get("judge_items")),
             _j(t.get("tool_calls")), _j(t.get("deliveries")),
             _j(t.get("flags"))))

    async def runs(self, limit: int = 10) -> list[dict[str, Any]]:
        return await self.db.fetch_all(
            "SELECT * FROM eval_runs WHERE status = 'completed' "
            "ORDER BY started_at DESC LIMIT ?", (limit,))

    async def get_run(self, run_ref: str) -> dict[str, Any] | None:
        """A run by id or unique id prefix."""
        rows = await self.db.fetch_all(
            "SELECT * FROM eval_runs WHERE id LIKE ? ORDER BY started_at DESC",
            (run_ref + "%",))
        return rows[0] if len(rows) == 1 else None

    async def trials(self, run_id: str) -> list[dict[str, Any]]:
        return await self.db.fetch_all(
            "SELECT * FROM eval_case_results WHERE run_id = ? "
            "ORDER BY case_id, repeat_index", (run_id,))

    async def trials_for_calibration(self, case_ids: list[str] | None = None,
                                     limit: int = 500) -> list[dict[str, Any]]:
        if case_ids:
            marks = ",".join("?" * len(case_ids))
            return await self.db.fetch_all(
                f"SELECT * FROM eval_case_results WHERE case_id IN ({marks}) "
                "AND input_messages_json IS NOT NULL "
                "ORDER BY created_at DESC LIMIT ?", (*case_ids, limit))
        return await self.db.fetch_all(
            "SELECT * FROM eval_case_results WHERE input_messages_json IS NOT NULL "
            "ORDER BY created_at DESC LIMIT ?", (limit,))

    async def insert_legacy(self, runs: list[dict[str, Any]],
                            results: list[dict[str, Any]]) -> tuple[int, int]:
        """Copy pre-v2 runs/results (read by the caller from the old DB
        file in sqlite read-only mode). Idempotent by id."""
        n_runs = n_res = 0
        for r in runs:
            cat = r.get("category") or ""
            model = cat.split("@", 1)[1] if "@" in cat else None
            if await self.db.fetch_one(
                    "SELECT 1 AS x FROM eval_runs WHERE id = ?", (r["id"],)):
                continue
            await self.db.execute(
                "INSERT INTO eval_runs (id, started_at, completed_at, category, "
                "total_cases, passed_cases, failed_cases, overall_pass_rate, "
                "status, model, repeats, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (r["id"], r["started_at"], r["completed_at"], r["category"],
                 r["total_cases"], r["passed_cases"], r["failed_cases"],
                 r["overall_pass_rate"], r["status"], model,
                 "imported from bob.db (pre-v2 harness, live-DB worlds)"))
            n_runs += 1
        for c in results:
            if await self.db.fetch_one(
                    "SELECT 1 AS x FROM eval_case_results WHERE id = ?", (c["id"],)):
                continue
            await self.db.execute(
                "INSERT INTO eval_case_results (id, run_id, case_id, category, "
                "passed, llm_response, llm_latency_seconds, judge_score, "
                "judge_reasoning, structural_results_json, error_message, "
                "created_at, input_messages_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (c["id"], c["run_id"], c["case_id"], c["category"], c["passed"],
                 c["llm_response"], c["llm_latency_seconds"], c["judge_score"],
                 c["judge_reasoning"], c["structural_results_json"],
                 c["error_message"], c["created_at"], c.get("input_messages_json")))
            n_res += 1
        return n_runs, n_res


def read_legacy(conn: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pre-v2 runs/results from an operator-opened READ-ONLY sqlite3
    connection to bob.db (the old harness stored them there)."""
    import sqlite3
    conn.row_factory = sqlite3.Row
    runs = [dict(r) for r in conn.execute("SELECT * FROM eval_runs").fetchall()]
    results = [dict(r) for r in conn.execute(
        "SELECT * FROM eval_case_results").fetchall()]
    return runs, results
