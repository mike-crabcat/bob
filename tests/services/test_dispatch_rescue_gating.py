"""Send-tool rescue gating → final-text delivery gating (2026-10-01,
docs/final-text-delivery-plan.md): a human-stimulus turn's final text IS
the reply and is delivered automatically; the send tool is for progress
updates and media. Turns claimed by system nudges alone still never
deliver — their un-sent text is internal bookkeeping, and delivering it
mails internal monologue to the chat (the "Folded: …" goal-state
summaries leaked to the AI doom group, 2026-08-29).

Pinned here, at the DispatchRunner seam:
- wake_nudge-only turn + un-sent text → NOT delivered, NOT recorded
- real inbound message + un-sent text → delivered (existing behaviour kept)
- mixed pending (nudge + real inbound) → delivered (a human is owed a reply)
- task_relay-only turn (background-task result) + un-sent text → delivered:
  relay turns exist to speak, so the send-skip quirk must not swallow the
  result (live 2026-08-30: a detached AFL turn's finished relay — holding
  ack sent, outcome never delivered — was silently dropped this way)
- a mid-turn PROGRESS send must not suppress the final answer (the old
  rescue's message_was_sent gate is gone — progress-then-answer is the
  intended shape)
- routine-category turns never deliver final text (Mike's scoping:
  silence is expected; routines speak only through explicit sends)
- a turn that already spoke must not re-deliver a trivial tail or a
  verbatim repeat (the duplicate-reply class of 2026-08-30/09-12)
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


# ---------------------------------------------------------------------------
# is_no_reply: prose must not trigger silence (2026-09-23 Mike-DM incident)
# ---------------------------------------------------------------------------

async def test_is_no_reply_prose_does_not_silence():
    """The reply '…one message from me, no reply from you yet' was swallowed
    twice (containment matched the plain-English phrase), the turn recorded
    message_was_sent=True, and the rescue stayed quiet — a succeeded turn
    that answered nothing."""
    from server.services.dispatch_runner import is_no_reply

    assert not is_no_reply("Yes — I can search and read full threads. One "
                           "message from me, no reply from you yet.")
    assert not is_no_reply("There's nothing to say about the price, but "
                           "here's the quote anyway.")
    # canonical token still containment-matched, decorated or bare
    assert is_no_reply("NO_REPLY")
    assert is_no_reply("[NO_REPLY — Simon asked for silence]")
    # plain-English variants only as the whole (bracket-stripped) message
    assert is_no_reply("No reply")
    assert is_no_reply("no reply.")
    assert is_no_reply("[NO REPLY]")
    assert is_no_reply("Nothing to say.")


# ---------------------------------------------------------------------------
# Final-text delivery (2026-10-01): the flip and its transition guards
# ---------------------------------------------------------------------------

async def test_progress_send_does_not_suppress_final_text(
        ctx, db, stub_llm, stub_history):
    """The load-bearing flip: a mid-turn progress update must not eat the
    final answer. The old rescue required `not message_was_sent` — under
    the progress-update contract that gate would swallow every
    progress-then-answer turn."""
    key = "test:final:progress-then-answer"
    svc = SessionService(ctx)
    await svc.add_message(key, "user", "how'd the render go?",
                          dispatched=0)
    send_tool = _FakeSendTool("send_whatsapp_message")
    spec = _spec(key, send_tool)
    # Emulate a mid-turn progress send (the real handler flips the flag
    # and appends to sent_texts when the model calls it).
    spec.message_was_sent[0] = True
    spec.sent_texts.append("still rendering — 6 of 8 frames done")
    stub_llm["reply"] = ("Done — the montage is at goals/1234/montage.mp4, "
                         "all 8 frames stitched cleanly.")

    await DispatchRunner(ctx).run(spec)

    assert send_tool.delivered == [stub_llm["reply"]], (
        "the final answer delivers even though the turn already sent "
        "a progress update")


async def test_routine_turn_final_text_never_delivered(
        ctx, db, stub_llm, stub_history):
    """Mike's scoping (2026-10-01): routines and wake-type calls expect
    silence — they speak only through explicit sends. A routine turn's
    final text must never auto-deliver (the 2026-09-17 routine
    confabulation class stays silent by default)."""
    key = "test:final:routine"
    svc = SessionService(ctx)
    await svc.add_message(key, "user", "morning brief payload",
                          dispatched=0, provenance="routine")
    send_tool = _FakeSendTool("send_whatsapp_message")
    spec = _spec(key, send_tool)
    spec.call_category = "routine"
    stub_llm["reply"] = "Brief composed internally, nothing to narrate."

    await DispatchRunner(ctx).run(spec)

    assert send_tool.delivered == [], (
        "routine-category turns never auto-deliver final text")


async def test_trivial_tail_after_send_not_delivered(
        ctx, db, stub_llm, stub_history):
    key = "test:final:trivial-tail"
    svc = SessionService(ctx)
    await svc.add_message(key, "user", "send me the file", dispatched=0)
    send_tool = _FakeSendTool("send_whatsapp_message")
    spec = _spec(key, send_tool)
    spec.message_was_sent[0] = True
    spec.sent_texts.append("Here's the rendered file, validated and ready.")
    stub_llm["reply"] = "Sent."

    await DispatchRunner(ctx).run(spec)

    assert send_tool.delivered == [], (
        "a ≤20-char tail after a real send is bookkeeping, not a reply")


async def test_verbatim_repeat_after_send_not_delivered(
        ctx, db, stub_llm, stub_history):
    """Transition guard: a model that still puts its ANSWER through the
    send tool (old habit, old conversations in context) and then repeats
    it as final text must not double-deliver (the duplicate-reply class
    of 2026-08-30/09-12). Paraphrases deliberately pass — the battery
    owns those."""
    key = "test:final:verbatim-repeat"
    svc = SessionService(ctx)
    await svc.add_message(key, "user", "what's the status?", dispatched=0)
    answer = ("All seven figurines are print-ready: meshes repaired, "
              "sliced at 0.4 nozzle, and the inspection renders passed "
              "for every one of them.")
    send_tool = _FakeSendTool("send_whatsapp_message")
    spec = _spec(key, send_tool)
    spec.message_was_sent[0] = True
    spec.sent_texts.append(answer)
    stub_llm["reply"] = answer  # verbatim repeat as final text

    await DispatchRunner(ctx).run(spec)

    assert send_tool.delivered == [], (
        "a verbatim repeat of an already-sent answer is not a new reply"
)


async def test_no_reply_final_text_stays_silent(
        ctx, db, stub_llm, stub_history):
    """The new silence primitive: an inbound turn finishing with the exact
    NO_REPLY marker delivers nothing."""
    key = "test:final:no-reply"
    svc = SessionService(ctx)
    await svc.add_message(key, "user", "group chatter not for Bob",
                          dispatched=0)
    send_tool = _FakeSendTool("send_whatsapp_message")
    stub_llm["reply"] = "NO_REPLY"

    await DispatchRunner(ctx).run(_spec(key, send_tool))

    assert send_tool.delivered == []
