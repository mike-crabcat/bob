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
- task_relay-only turn (background-task result) + un-sent text → rescued:
  relay turns exist to speak, so the send-skip quirk must not swallow the
  result (live 2026-08-30: a detached AFL turn's finished relay — holding
  ack sent, outcome never delivered — was silently dropped this way)
"""

from __future__ import annotations

import pytest

from server.services.dispatch_runner import DispatchRunner, DispatchSpec
from server.services.session_service import SessionService


class _FakeSendTool:
    def __init__(self, name: str, spec: DispatchSpec | None = None):
        self.name = name
        self.spec = spec
        self.delivered: list[str] = []

    async def handler(self, text: str) -> str:
        self.delivered.append(text)
        # Emulate the real handler's bookkeeping: the runner's relay
        # dead-man switch reads spec.sent_texts to tell a delivered turn
        # from a silent one.
        if self.spec is not None:
            self.spec.message_was_sent[0] = True
            self.spec.sent_texts.append(text)
        return "sent"


def _spec(session_key: str, send_tool: _FakeSendTool | None = None) -> DispatchSpec:
    send_tool = send_tool or _FakeSendTool("send_whatsapp_message")
    spec = DispatchSpec(
        session_key=session_key,
        system_content="system",
        tools=[send_tool],
        call_category="whatsapp_incoming",
        send_tool_name=send_tool.name,
        dispatch_id="test-dispatch",
        message_was_sent=[False],
        sent_texts=[],
    )
    send_tool.spec = spec
    return spec


@pytest.fixture
def stub_llm(monkeypatch):
    """chat_with_tools returns un-sent text; captures the built messages.
    Swap the canned reply via ``stub_llm["reply"] = …``."""
    from server.services.llm_dispatch import LLMDispatchService
    seen: dict = {"reply": "Folded. Blair's preferences recorded; no group post."}

    async def _chat_with_tools(self, messages, tools, **kwargs):
        seen["messages"] = messages
        return seen["reply"]

    monkeypatch.setattr(LLMDispatchService, "chat_with_tools", _chat_with_tools)
    return seen


@pytest.fixture
def stub_history(monkeypatch):
    async def _build(dummy, session_key, **kwargs):
        return [{"role": "user", "content": "nudge"}]

    monkeypatch.setattr(
        "server.services.prompt_assembler.build_chat_messages", _build)


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


async def test_task_relay_only_turn_unsent_text_is_rescued(
        ctx, db, stub_llm, stub_history):
    """A background-task relay is a system nudge that MUST speak — the
    rescue covers it when the model writes the relay as un-sent text."""
    key = "test:rescue:task-relay"
    svc = SessionService(ctx)
    await svc.add_message(
        key, "user",
        "[Background task abcd1234] The Grand has rooms Thursday.\n\n"
        "Relay the result to the user with a short summary in your own voice.",
        dispatched=0, provenance="task_relay")
    send_tool = _FakeSendTool("send_whatsapp_message")
    stub_llm["reply"] = ("Honest answer: 5 of 12 tracks are in, the matcher "
                         "is sulking on the rest.")

    await DispatchRunner(ctx).run(_spec(key, send_tool))

    assert send_tool.delivered == [stub_llm["reply"]]


# ---------------------------------------------------------------------------
# Echo guard (2026-09-14 Mike-DM incident)
# ---------------------------------------------------------------------------

async def test_marker_parrot_not_rescued(ctx, db, stub_llm, stub_history):
    """GLM returned the marked user line verbatim as final text with no send
    call; the rescue mailed Mike his own question back. A reply carrying the
    new-message marker — or exactly echoing the stimulus — is never
    delivered."""
    key = "test:rescue:echo-marker"
    svc = SessionService(ctx)
    await svc.add_message(
        key, "user",
        "Tell me a little more about yourself. What motivates you? What is your life goal?",
        dispatched=0)
    send_tool = _FakeSendTool("send_whatsapp_message")
    stub_llm["reply"] = ("[NEW — awaiting your reply] Tell me a little more about "
                         "yourself. What motivates you? What is your life goal?")

    await DispatchRunner(ctx).run(_spec(key, send_tool))

    assert send_tool.delivered == [], "marker parrot must not be delivered"


async def test_verbatim_echo_without_marker_not_rescued(
        ctx, db, stub_llm, stub_history):
    key = "test:rescue:echo-bare"
    svc = SessionService(ctx)
    await svc.add_message(key, "user", "what time is it", dispatched=0)
    send_tool = _FakeSendTool("send_whatsapp_message")
    stub_llm["reply"] = "What time is it"  # case/whitespace-insensitive match

    await DispatchRunner(ctx).run(_spec(key, send_tool))

    assert send_tool.delivered == [], "verbatim echo must not be delivered"


async def test_normal_reply_still_rescued_alongside_guard(
        ctx, db, stub_llm, stub_history):
    """The guard is exact-match only — a real answer (even quoting a word of
    the question) still gets rescued."""
    key = "test:rescue:echo-false-positive"
    svc = SessionService(ctx)
    await svc.add_message(key, "user", "what time is it", dispatched=0)
    send_tool = _FakeSendTool("send_whatsapp_message")
    stub_llm["reply"] = "It's 3pm WST — I checked the clock, not the vibes."

    await DispatchRunner(ctx).run(_spec(key, send_tool))

    assert send_tool.delivered == [stub_llm["reply"]]


async def test_marker_swap_parrot_not_rescued(ctx, db, stub_llm, stub_history):
    """2026-09-16 variant: GLM swapped the [NEW — awaiting your reply] marker
    for its own bracketed tag ('[Proposing, as my bit…] …') and echoed the
    line otherwise verbatim; the exact-match guard missed it and the rescue
    mailed Mike his own approval back. Leading bracketed segments are now
    stripped before the comparison."""
    key = "test:rescue:echo-marker-swap"
    svc = SessionService(ctx)
    await svc.add_message(
        key, "user",
        "I approve it to be global. Is that something you can act on?",
        dispatched=0)
    send_tool = _FakeSendTool("send_whatsapp_message")
    stub_llm["reply"] = ("[Proposing, as my bit…] I approve it to be global. "
                         "Is that something you can act on?")

    await DispatchRunner(ctx).run(_spec(key, send_tool))

    assert send_tool.delivered == [], "marker-swap parrot must not be delivered"


async def test_bracketed_prefix_with_real_reply_still_rescued(
        ctx, db, stub_llm, stub_history):
    """A leading bracketed tag on a genuinely different reply is fine — only
    tag + verbatim-echo is a parrot."""
    key = "test:rescue:echo-tag-real"
    svc = SessionService(ctx)
    await svc.add_message(
        key, "user", "I approve it to be global. Is that something you can act on?",
        dispatched=0)
    send_tool = _FakeSendTool("send_whatsapp_message")
    stub_llm["reply"] = ("[done] Flipped zai-web-search to global — every "
                         "conversation gets web_search_prime now.")

    await DispatchRunner(ctx).run(_spec(key, send_tool))

    assert send_tool.delivered == [stub_llm["reply"]]
