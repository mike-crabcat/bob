"""LLM call log repository — owns all llm_call_log SQL.

Telemetry table: one row per LLM API call (tokens, latency, status,
payloads). Payloads are redacted after 30 days by the retention sweep;
metric columns are kept forever.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4


class LlmCallLogRepository:
    def __init__(self, db: Any):
        self.db = db

    async def upsert(
        self,
        *,
        log_id: str | None = None,
        provider: str = "",
        model: str = "",
        call_category: str = "",
        session_key: str | None = None,
        system_prompt: str = "",
        user_message: str = "",
        messages_json: str | None = None,
        tools_json: str | None = None,
        response_text: str = "",
        latency_seconds: float | None = None,
        ttft_seconds: float | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
        cached_tokens: int | None = None,
        status: str = "completed",
        error_message: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        dispatch_id: str | None = None,
        contact_id: str | None = None,
        tool_blocks_json: str | None = None,
    ) -> str:
        """Record or update a call log entry; returns the log id.

        If log_id names an existing row it is UPDATEd (completion of a
        'running' row), otherwise a new row is INSERTed."""
        if log_id is not None:
            existing = await self.db.fetch_one(
                "SELECT id FROM llm_call_log WHERE id = ?", (log_id,))
            if existing:
                await self.db.execute(
                    """UPDATE llm_call_log SET
                       response_text=?, latency_seconds=?, ttft_seconds=?,
                       prompt_tokens=?, completion_tokens=?, total_tokens=?, cached_tokens=?,
                       status=?, error_message=?, messages_json=COALESCE(?, messages_json),
                       tool_blocks_json=COALESCE(?, tool_blocks_json)
                       WHERE id = ?""",
                    (response_text, latency_seconds, ttft_seconds,
                     prompt_tokens, completion_tokens, total_tokens, cached_tokens,
                     status, error_message, messages_json, tool_blocks_json, log_id))
                return log_id

        row_id = log_id or str(uuid4())
        await self.db.execute(
            """INSERT INTO llm_call_log
               (id, provider, model, call_category, session_key,
                system_prompt, user_message, messages_json, tools_json,
                response_text, latency_seconds, ttft_seconds,
                prompt_tokens, completion_tokens, total_tokens, cached_tokens,
                status, error_message, project_id, task_id, dispatch_id, contact_id,
                tool_blocks_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row_id, provider, model, call_category, session_key,
             system_prompt, user_message, messages_json, tools_json,
             response_text, latency_seconds, ttft_seconds,
             prompt_tokens, completion_tokens, total_tokens, cached_tokens,
             status, error_message, project_id, task_id, dispatch_id, contact_id,
             tool_blocks_json))
        return row_id

    # -------------------------------------------------------- maintenance

    async def redact_payloads_before(self, cutoff_iso: str) -> int:
        """Strip heavy payload columns from rows older than the cutoff."""
        return await self.db.execute(
            """UPDATE llm_call_log
               SET system_prompt = '', user_message = '', messages_json = NULL,
                   response_text = '', tools_json = NULL, tool_blocks_json = NULL
               WHERE created_at < ?
                 AND (length(system_prompt) > 0 OR length(user_message) > 0
                      OR messages_json IS NOT NULL OR length(response_text) > 0
                      OR tools_json IS NOT NULL OR tool_blocks_json IS NOT NULL)""",
            (cutoff_iso,))

    async def fail_stale_running(self, *, stale_minutes: int) -> int:
        return await self.db.execute(
            "UPDATE llm_call_log SET status = 'failed', error_message = 'Stale running call — timed out' "
            "WHERE status = 'running' AND created_at < datetime('now', ?)",
            (f"-{stale_minutes} minutes",))

    # ------------------------------------------------------------- reads

    async def get(self, call_id: str) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            """SELECT id, created_at, provider, model, call_category, session_key,
                      system_prompt, user_message, messages_json, tools_json,
                      response_text, latency_seconds, ttft_seconds,
                      prompt_tokens, completion_tokens, total_tokens, cached_tokens,
                      status, error_message, tool_blocks_json
               FROM llm_call_log WHERE id = ?""",
            (call_id,))
        return dict(row) if row else None

    async def probe_decisions(
        self, session_keys: list[str], *, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Tier-2 attention-probe calls for a set of session keys."""
        if not session_keys:
            return []
        marks = ",".join("?" * len(session_keys))
        rows = await self.db.fetch_all(
            f"""SELECT created_at, response_text, status, latency_seconds
                FROM llm_call_log
                WHERE call_category = 'attention_probe' AND session_key IN ({marks})
                ORDER BY created_at DESC LIMIT ?""",
            (*session_keys, limit))
        return [dict(r) for r in rows] if rows else []

    async def session_calls_with_contact(
        self, session_key: str, *, limit: int = 100,
    ) -> list[dict[str, Any]]:
        """A session's recent calls with the caller contact's name resolved."""
        rows = await self.db.fetch_all(
            """SELECT l.id, l.created_at, l.call_category, l.status, l.latency_seconds,
                      l.ttft_seconds, l.total_tokens, l.prompt_tokens, l.completion_tokens,
                      l.tool_blocks_json, l.user_message, l.response_text,
                      l.error_message, l.contact_id, l.model,
                      c.name as contact_name
               FROM llm_call_log l
               LEFT JOIN contacts c ON c.id = l.contact_id AND c.deleted_at IS NULL
               WHERE l.session_key = ?
               ORDER BY l.created_at DESC
               LIMIT ?""",
            (session_key, limit))
        return [dict(r) for r in rows] if rows else []

    async def activity_rollup(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            """SELECT session_key,
                      COUNT(*) as call_count,
                      MAX(created_at) || 'Z' as last_activity,
                      SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed,
                      SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed,
                      ROUND(AVG(CASE WHEN latency_seconds IS NOT NULL THEN latency_seconds END), 2) as avg_latency
               FROM llm_call_log
               WHERE session_key IS NOT NULL
               GROUP BY session_key
               ORDER BY last_activity DESC
               LIMIT ?""", (limit,))
        return [dict(r) for r in rows] if rows else []

    async def category_chart_24h(self) -> list[dict[str, Any]]:
        """15-minute-bucketed call counts per category, quota failures excluded."""
        rows = await self.db.fetch_all(
            """SELECT
                  strftime('%Y-%m-%dT%H:%M',
                      datetime(strftime('%s', created_at) - strftime('%s', created_at) % 900, 'unixepoch')
                  ) as interval_start,
                  call_category,
                  COUNT(*) as count
               FROM llm_call_log
               WHERE created_at >= datetime('now', '-24 hours')
                 AND NOT (status = 'failed' AND (
                     error_message LIKE '%insufficient_quota%'
                     OR error_message LIKE '%credit_balance_exhausted%'))
               GROUP BY interval_start, call_category
               ORDER BY interval_start""")
        return [dict(r) for r in rows] if rows else []

    async def cost_rollup_24h(self) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            """SELECT call_category, model, provider,
                      SUM(COALESCE(prompt_tokens, 0)) as total_prompt_tokens,
                      SUM(COALESCE(completion_tokens, 0)) as total_completion_tokens,
                      SUM(COALESCE(cached_tokens, 0)) as total_cached_tokens,
                      COUNT(*) as call_count
               FROM llm_call_log
               WHERE created_at >= datetime('now', '-24 hours')
                 AND NOT (status = 'failed' AND (
                     error_message LIKE '%insufficient_quota%'
                     OR error_message LIKE '%credit_balance_exhausted%'))
               GROUP BY call_category, model, provider
               ORDER BY call_category, model""")
        return [dict(r) for r in rows] if rows else []

    async def recent_for_transcript(
        self, session_key: str, *, limit: int,
    ) -> list[dict[str, Any]]:
        """Latest N calls, returned oldest-first (reflection transcripts)."""
        rows = await self.db.fetch_all(
            """SELECT * FROM (
                   SELECT id, call_category, status, model, user_message, response_text,
                          error_message, messages_json, created_at
                   FROM llm_call_log
                   WHERE session_key = ?
                   ORDER BY created_at DESC
                   LIMIT ?
               ) ORDER BY created_at ASC""",
            (session_key, limit))
        return [dict(r) for r in rows] if rows else []
