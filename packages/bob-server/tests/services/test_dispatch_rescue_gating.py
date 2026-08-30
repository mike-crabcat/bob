"""Send-tool rescue gating: the rescue exists to recover replies the model
wrote but forgot to send (GLM-5.3 skips the final send call on ~20% of
turns). But a turn claimed by system nudges alone isn't expected to speak —
its un-sent final text is internal bookkeeping, and rescuing it mails
internal monologue to the chat (the "Folded: …" goal-state summaries leaked
to the AI doom group, 2026-08-29).

Pinned here, at the DispatchRunner seam:
- wake_nudge-only turn + un-sent text → NOT delivered, NOT recorded
- real inbound message + un-sent text → rescued (existing behaviour kept)
- mixed pending (nudge + real inbound) → rescued (a human is owed a reply)
"""

from __future__ import annotations

import pytest

from bob_server.services.dispatch_runner import DispatchRunner, DispatchSpec
from bob_server.services.session_service import SessionService


class _FakeSendTool:
    def __init__(self, name: str):
        self.name = name
        self.delivered: list[str] = []

    async def handler(self, text: str) -> str:
        self.delivered.append(text)
        return "sent"


def _spec(session_key: str, send_tool: _FakeSendTool) -> DispatchSpec:
    return DispatchSpec(
        session_key=session_key,
        system_content="system",
        tools=[send_tool],
        call_category="whatsapp_incoming",
        send_tool_name=send_tool.name,
        dispatch_id="test-dispatch",
        message_was_sent=[False],
        sent_texts=[],
    )


@pytest.fixture
def stub_llm(monkeypatch):
    """chat_with_tools returns un-sent text; captures the built messages."""
    from bob_server.services.llm_dispatch import LLMDispatchService
    seen: dict = {}

    async def _chat_with_tools(self, messages, tools, **kwargs):
        seen["messages"] = messages
        return "Folded. Blair's preferences recorded; no group post."

    monkeypatch.setattr(LLMDispatchService, "chat_with_tools", _chat_with_tools)
    return seen


@pytest.fixture
def stub_history(monkeypatch):
    async def _build(dummy, session_key, **kwargs):
        return [{"role": "user", "content": "nudge"}]

    monkeypatch.setattr(
        "bob_server.services.prompt_assembler.build_chat_messages", _build)


async def _assistant_rows(db, session_key: str) -> list:
    return await db.fetch_all(
        "SELECT content FROM messages WHERE conversation_id = ? AND role = 'assistant'",
        (session_key,))


async def test_nudge_only_turn_unsent_text_not_rescued(
        ctx, db, stub_llm, stub_history):
    key = "test:rescue:nudge-only"
    svc = SessionService(ctx)
    await svc.add_message(key, "user", "## Goal progress\nFold it yourself.",
                          dispatched=0, provenance="wake_nudge")
    send_tool = _FakeSendTool("send_whatsapp_message")

    await DispatchRunner(ctx).run(_spec(key, send_tool))

    assert send_tool.delivered == [], "internal fold text must not be delivered"
    assert await _assistant_rows(db, key) == [], "delivered-only: nothing recorded"


async def test_real_inbound_turn_unsent_text_is_rescued(
        ctx, db, stub_llm, stub_history):
    key = "test:rescue:inbound"
    svc = SessionService(ctx)
    await svc.add_message(key, "user", "hello from a human", dispatched=0)
    send_tool = _FakeSendTool("send_whatsapp_message")

    await DispatchRunner(ctx).run(_spec(key, send_tool))

    assert send_tool.delivered == ["Folded. Blair's preferences recorded; no group post."]


async def test_mixed_nudge_and_inbound_is_rescued(
        ctx, db, stub_llm, stub_history):
    """A nudge racing a real inbound keeps the rescue — the human message
    still deserves a reply."""
    key = "test:rescue:mixed"
    svc = SessionService(ctx)
    await svc.add_message(key, "user", "## Goal progress\nnudge",
                          dispatched=0, provenance="wake_nudge")
    await svc.add_message(key, "user", "actual question", dispatched=0)
    send_tool = _FakeSendTool("send_whatsapp_message")

    await DispatchRunner(ctx).run(_spec(key, send_tool))

    assert len(send_tool.delivered) == 1
