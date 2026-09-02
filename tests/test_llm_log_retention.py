"""Tests for LlmLogRetentionTask payload redaction."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import server.heartbeat as heartbeat
from server.heartbeat import LlmLogRetentionTask


async def _insert_call(db, call_id: str, created_at: str) -> None:
    await db.execute(
        """INSERT INTO llm_call_log
           (id, created_at, provider, call_category, system_prompt, user_message,
            messages_json, response_text, tools_json, tool_blocks_json,
            prompt_tokens, completion_tokens)
           VALUES (?, ?, 'openai', 'chat', 'sys', 'hello', '[]', 'world', '[]', '[]', 10, 5)""",
        (call_id, created_at),
    )


@pytest.mark.asyncio
async def test_redacts_old_payloads_keeps_rows_and_metrics(ctx):
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=45)).strftime("%Y-%m-%d %H:%M:%S")
    recent = (now - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
    await _insert_call(ctx.db, "old-call", old)
    await _insert_call(ctx.db, "recent-call", recent)

    heartbeat._last_llm_log_redaction = None
    await LlmLogRetentionTask().run(ctx)

    old_row = await ctx.db.fetch_one("SELECT * FROM llm_call_log WHERE id = 'old-call'")
    assert old_row is not None, "row must be kept forever"
    assert old_row["system_prompt"] == ""
    assert old_row["user_message"] == ""
    assert old_row["messages_json"] is None
    assert old_row["response_text"] == ""
    assert old_row["tools_json"] is None
    assert old_row["tool_blocks_json"] is None
    assert old_row["prompt_tokens"] == 10, "metrics must survive"
    assert old_row["completion_tokens"] == 5

    recent_row = await ctx.db.fetch_one("SELECT * FROM llm_call_log WHERE id = 'recent-call'")
    assert recent_row["user_message"] == "hello", "recent payloads untouched"
    assert recent_row["response_text"] == "world"


@pytest.mark.asyncio
async def test_throttles_to_daily(ctx):
    heartbeat._last_llm_log_redaction = None
    task = LlmLogRetentionTask()
    await task.run(ctx)
    first_run = heartbeat._last_llm_log_redaction
    assert first_run is not None

    await task.run(ctx)
    assert heartbeat._last_llm_log_redaction == first_run, "second run within 24h must be a no-op"


@pytest.mark.asyncio
async def test_boot_sweep_cancels_zombie_running_rows(ctx):
    """At startup every 'running' row is a zombie (nothing from the previous
    process survives a restart). The sweep cancels exactly those — completed
    and failed rows untouched, including the staleness task's own 'failed'."""
    from server.repositories.llm_call_log import LlmCallLogRepository

    now = datetime.now(timezone.utc)
    await _insert_call(ctx.db, "zombie-call", now.strftime("%Y-%m-%d %H:%M:%S"))
    await db_set_status(ctx.db, "zombie-call", "running")
    await _insert_call(ctx.db, "done-call", now.strftime("%Y-%m-%d %H:%M:%S"))
    await db_set_status(ctx.db, "done-call", "completed")

    swept = await LlmCallLogRepository(ctx.db).cancel_running()
    assert swept == 1

    zombie = await ctx.db.fetch_one(
        "SELECT status, error_message FROM llm_call_log WHERE id = 'zombie-call'")
    assert zombie["status"] == "cancelled"
    assert zombie["error_message"] == "Cancelled — server restart"
    done = await ctx.db.fetch_one(
        "SELECT status FROM llm_call_log WHERE id = 'done-call'")
    assert done["status"] == "completed"


async def db_set_status(db, call_id: str, status: str) -> None:
    await db.execute(
        "UPDATE llm_call_log SET status = ? WHERE id = ?", (status, call_id))
