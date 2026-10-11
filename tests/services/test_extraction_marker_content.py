"""Extraction marker rows never carry model prose (2026-10-06 incident).

The idle memory-extraction turn used to store the model's final text on its
marker row when present. GLM ended one such turn with "Recorded: task to
remind Mike about Rottnest…" — a first-person Bob-voice claim of work that
was never done, sitting in history surfaces (extraction prompts, session
message reads, dashboard) as if Bob had said it. The marker row is machine
bookkeeping that claims' source_messages reference; its content is always
the bracketed summary, and the model's text lives in llm_call_log."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from server.services.memory.service import MemoryService
from server.services.session_service import SessionService

NOW = datetime.now(timezone.utc)

PROSE = "Recorded: task to remind Mike about Rottnest at 12pm tomorrow."


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


async def _seed(ctx, key: str) -> None:
    ss = SessionService(ctx)
    mid = await ss.add_message(key, "user", "blair asks for a rottnest reminder")
    await ctx.db.execute(
        "UPDATE messages SET created_at = ? WHERE id = ?",
        (_iso(NOW - timedelta(minutes=5)), mid),
    )


@pytest.mark.asyncio
async def test_marker_row_gets_bracketed_summary_not_model_text(ctx, monkeypatch):
    """The extractor's conversational summary must not land as a chat row."""
    from server.services.llm_dispatch import LLMDispatchService

    async def fake_chat(self, messages, tools, **kw):
        return PROSE  # model claims it recorded something; it did not

    monkeypatch.setattr(LLMDispatchService, "run_turn", fake_chat)

    key = "agent:main:whatsapp:dm:61000000003"
    await _seed(ctx, key)
    result = await MemoryService(ctx).run_silent_turn_extraction(key)
    assert result["status"] == "ok"

    row = await ctx.db.fetch_one(
        "SELECT content FROM messages WHERE id = ?",
        (result["turn_message_id"],),
    )
    assert row is not None
    assert row["content"] == "[Silent extraction turn: nothing memory-worthy]"
    assert PROSE not in row["content"]


@pytest.mark.asyncio
async def test_no_message_row_anywhere_carries_extractor_prose(ctx, monkeypatch):
    """Belt and braces: the prose appears on no row in the session."""
    from server.services.llm_dispatch import LLMDispatchService

    async def fake_chat(self, messages, tools, **kw):
        return PROSE

    monkeypatch.setattr(LLMDispatchService, "run_turn", fake_chat)

    key = "agent:main:whatsapp:dm:61000000004"
    await _seed(ctx, key)
    await MemoryService(ctx).run_silent_turn_extraction(key)

    rows = await ctx.db.fetch_all(
        "SELECT content FROM messages WHERE conversation_id = ?", (key,))
    assert rows
    for r in rows:
        assert PROSE not in r["content"]
