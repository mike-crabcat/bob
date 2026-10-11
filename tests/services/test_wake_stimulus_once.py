"""Generic wake dispatch shows the stimulus ONCE (2026-10-08 eval finding:
wake_conversation stores the row, then the dispatch also passed it as
user_message — every room/utility wake saw its stimulus twice)."""

from __future__ import annotations

import asyncio

from server.services import wake_service
from server.services.llm_dispatch import LLMDispatchService


async def test_generic_wake_renders_stimulus_once(ctx, monkeypatch):
    ctx.settings.openai.api_key = ctx.settings.openai.api_key or "sk-test"
    seen: list[list] = []

    async def fake_run_turn(self, messages, tools, **kw):
        seen.append(list(messages))
        return "ok"

    monkeypatch.setattr(LLMDispatchService, "run_turn", fake_run_turn)
    content = "[bg process montage] exit_code=0 — 4 files rendered"
    armed = await wake_service.wake_conversation(
        ctx, "conv-internal-test", content, call_category="subagent_result")
    assert armed
    await asyncio.gather(*list(wake_service._pending_dispatches))
    assert seen, "no turn ran"
    hits = [m for m in seen[0] if isinstance(m, dict)
            and content in str(m.get("content", ""))]
    assert len(hits) == 1, hits
    assert seen[0][-1] is hits[0], "the stimulus is still the last message"
