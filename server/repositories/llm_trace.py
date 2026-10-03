"""LLM trace repository — owns all llm_trace_events SQL.

Trace table (2026-10-03 uplift): the durable per-round timeline of an LLM
turn — reasoning parts, tool calls/results, round boundaries with latency —
written incrementally by llm_dispatch while the turn runs. Content columns
are stripped after 30 days (aligned with llm_call_log payload redaction);
rows hard-delete at 90d via the llm_call_log_cleanup trigger's cascade.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4


class LlmTraceRepository:
    def __init__(self, db: Any):
        self.db = db

    async def append(
        self,
        *,
        llm_call_id: str,
        seq: int,
        kind: str,
        iteration: int = 0,
        dispatch_id: str | None = None,
        session_key: str | None = None,
        content: str = "",
        meta: dict[str, Any] | None = None,
    ) -> str:
        """Append one timeline event row. Returns the row id."""
        row_id = str(uuid4())
        await self.db.execute(
            """INSERT INTO llm_trace_events
               (id, llm_call_id, dispatch_id, session_key, iteration, seq,
                kind, content, meta_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row_id, llm_call_id, dispatch_id, session_key, iteration, seq,
             kind, content,
             json.dumps(meta, default=str) if meta else None))
        return row_id

    async def append_many(
        self,
        *,
        llm_call_id: str,
        events: list[dict[str, Any]],
    ) -> int:
        """Append a batch of pre-shaped event dicts (keys as in append()).

        Writers coalesce per-round rows in memory and flush once, so a turn
        with N rounds costs N DB writes, not 5N."""
        if not events:
            return 0
        rows = [
            (str(uuid4()), llm_call_id, e.get("dispatch_id"), e.get("session_key"),
             e.get("iteration", 0), e["seq"], e["kind"], e.get("content", ""),
             json.dumps(e["meta"], default=str) if e.get("meta") else None)
            for e in events
        ]
        await self.db.execute_many(
            """INSERT INTO llm_trace_events
               (id, llm_call_id, dispatch_id, session_key, iteration, seq,
                kind, content, meta_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows)
        return len(rows)

    async def for_call(self, call_id: str) -> list[dict[str, Any]]:
        """The full timeline for a call, seq-ordered (dashboard trace view)."""
        rows = await self.db.fetch_all(
            """SELECT id, iteration, seq, kind, content, meta_json, created_at
               FROM llm_trace_events
               WHERE llm_call_id = ?
               ORDER BY seq ASC""",
            (call_id,))
        out: list[dict[str, Any]] = []
        for r in rows or []:
            d = dict(r)
            if d.get("meta_json"):
                try:
                    d["meta"] = json.loads(d.pop("meta_json"))
                except (json.JSONDecodeError, TypeError):
                    d.pop("meta_json", None)
                    d["meta"] = None
            else:
                d.pop("meta_json", None)
                d["meta"] = None
            out.append(d)
        return out

    async def count_for_call(self, call_id: str) -> int:
        row = await self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM llm_trace_events WHERE llm_call_id = ?",
            (call_id,))
        return int(row["n"]) if row else 0

    # -------------------------------------------------------- maintenance

    async def redact_content_before(self, cutoff_iso: str) -> int:
        """Strip payload content from rows older than the cutoff (30d, aligned
        with llm_call_log redaction — LlmLogRetentionTask drives both)."""
        return await self.db.execute(
            """UPDATE llm_trace_events
               SET content = '', meta_json = NULL
               WHERE created_at < ?
                 AND (length(content) > 0 OR meta_json IS NOT NULL)""",
            (cutoff_iso,))
