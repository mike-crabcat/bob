"""Relay-hop hardening, three incidents in one place (the backburner relay
is the only channel where a speak-worthy result can vanish or arrive as
soup):

1. 2026-09-03 (AI doom): the relay turn NO_REPLIED off the payload's own
   "reply sent" summary — Andrew's answer evaporated silently. Fixed by the
   delivery-truth relay template + the runner's dead-man rescue.
2. 2026-09-04 (Bob-management): GLM emitted its send call as <tool_call> XML
   text in an arg_key/arg_value dialect the Hermes recovery parser doesn't
   know; upstream parsing ate the opening half and the send-tool rescue
   delivered the orphaned tail verbatim. Fixed by strip_leaked_tool_xml +
   the markup-only refusal in the send tool.
"""

from __future__ import annotations

import pytest

from server.services.backburner import BackburnerService
from server.services.dispatch_runner import DispatchRunner, DispatchSpec
from server.services.openai_service import (
    strip_leaked_tool_xml, _strip_hermes_tool_calls)
from server.services.session_service import SessionService


class _FakeSendTool:
    def __init__(self, name: str):
        self.name = name
        self.spec: DispatchSpec | None = None
        self.delivered: list[str] = []

    async def handler(self, text: str) -> str:
        self.delivered.append(text)
        if self.spec is not None:
            self.spec.message_was_sent[0] = True
            self.spec.sent_texts.append(text)
        return "sent"


def _spec(session_key: str) -> tuple[DispatchSpec, _FakeSendTool]:
    send_tool = _FakeSendTool("send_whatsapp_message")
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
    return spec, send_tool


# ---------------------------------------------------------------- stripping

def test_leaked_full_span_removed_entirely():
    """A GLM-dialect span is a malformed call attempt, not prose."""
    text = ('<tool_call><arg_key>text</arg_key><arg_value>send this'
            "</arg_value></tool_call>")
    assert strip_leaked_tool_xml(text) == ""


def test_leaked_orphan_tail_keeps_prose():
    """The 2026-09-04 leak shape: upstream ate the opening, the tail is the
    only surviving text — tags go, the payload stays."""
    text = ("</arg_key><arg_value>Objective complete: video shared with "
            "Blair.</arg_value></tool_call>")
    assert strip_leaked_tool_xml(text) == (
        "Objective complete: video shared with Blair.")


def test_plain_text_untouched():
    text = "Normal message, some math: x < 5 and y > 2. Done."
    assert strip_leaked_tool_xml(text) == text


def test_prose_with_leaked_tail_is_cleaned():
    text = ("Sure thing. </tool_name><arg_value>here is the answer"
            "</arg_value></tool_call>")
    cleaned = strip_leaked_tool_xml(text)
    assert "arg_value" not in cleaned
    assert "here is the answer" in cleaned


def test_hermes_strip_handles_unknown_dialects():
    """_strip_hermes_tool_calls (replay + final-text cleanup) must remove
    non-Hermes spans too, not just the recoverable shape."""
    text = ("Answer follows. <tool_call><arg_key>x</arg_key>"
            "<arg_value>y</arg_value></tool_call> Done.")
    assert _strip_hermes_tool_calls(text) == "Answer follows."


# ------------------------------------------------------------ fallback content

def test_fallback_content_is_context_not_instruction():
    """v2 (docs/detach-v2.md): the silent-flight fallback carries the result
    as CONTEXT and forbids redo — the v1 'nothing was delivered, deliver it'
    preamble is the documented duplicate-work mechanism (2026-09-10
    double-sell, 2026-09-11 double-gif) and must never return."""
    content = BackburnerService._fallback_content(
        "abcd1234", "Reply sent to Andrew with the sources.")
    assert "finished without posting" in content
    assert "do NOT" in content
    assert "nothing in it has been delivered" not in content
    # The boilerplate tails relay_payload splits on, verbatim:
    assert "\n\nTell Mike briefly" in content
    assert content.index("finished without posting") < content.index(
        "Reply sent to Andrew"), "context leads, payload follows"


def test_failed_content_names_real_effects():
    content = BackburnerService._failed_content("abcd1234", "it broke")
    assert "may have had real effects" in content
    assert "\n\nIts tool calls before failing" in content


# ------------------------------------------------------------- dead-man arc

@pytest.fixture
def stub_llm(monkeypatch):
    from server.services.llm_dispatch import LLMDispatchService
    seen: dict = {"reply": "NO_REPLY"}

    async def _chat_with_tools(self, messages, tools, **kwargs):
        return seen["reply"]

    monkeypatch.setattr(LLMDispatchService, "chat_with_tools", _chat_with_tools)
    return seen


@pytest.fixture
def stub_history(monkeypatch):
    async def _build(dummy, session_key, **kwargs):
        return [{"role": "user", "content": "relay"}]

    monkeypatch.setattr(
        "server.services.prompt_assembler.build_chat_messages", _build)


async def test_relay_no_reply_dead_man_delivers_payload(
        ctx, db, stub_llm, stub_history):
    """The 2026-09-03 incident, v2 shape: the fallback turn answers
    NO_REPLY anyway. The runner delivers the payload itself."""
    key = "test:relay:deadman"
    svc = SessionService(ctx)
    await svc.add_message(
        key, "user",
        "[bg task abcd1234] finished without posting anything. Everything "
        "it did via tools already happened for real — do NOT redo it. Its "
        "result, for context:\n\n"
        "Reply sent to Andrew with the deep-dive sources: METR report et al.\n\n"
        "Tell Mike briefly what came of it if anything here is worth saying; "
        "silence is fine for routine work.",
        dispatched=0, provenance="task_relay")
    spec, send_tool = _spec(key)

    await DispatchRunner(ctx).run(spec)

    assert send_tool.delivered == [
        "(auto-delivered background result)\n"
        "Reply sent to Andrew with the deep-dive sources: METR report et al."]


async def test_relay_delivered_turn_no_dead_man(
        ctx, db, stub_llm, stub_history):
    """A fallback turn that speaks (via the normal rescue) must not ALSO get
    the dead-man payload — one delivery, not two."""
    key = "test:relay:spoken"
    stub_llm["reply"] = "Andrew — here are the sources you wanted."
    svc = SessionService(ctx)
    await svc.add_message(
        key, "user",
        "[bg task abcd1234] finished without posting anything. Its result, "
        "for context:\n\nresult body\n\n"
        "Tell Mike briefly what came of it.",
        dispatched=0, provenance="task_relay")
    spec, send_tool = _spec(key)

    await DispatchRunner(ctx).run(spec)

    assert send_tool.delivered == ["Andrew — here are the sources you wanted."]


async def test_relay_payload_strips_boilerplate_and_caps(ctx, db):
    from server.repositories.history import HistoryRepository
    key = "test:relay:payload"
    svc = SessionService(ctx)
    # Real v2 fallback shape: header paragraph, blank line, result, blank
    # line, trailing directive. The header must NOT survive into the
    # delivered payload (2026-09-06: the v1 rescue mailed its header to the
    # Bob Security Guard group verbatim).
    await svc.add_message(
        key, "user",
        "[bg task abcd1234] finished without posting anything. Everything "
        "it did via tools already happened for real — do NOT redo it.\n\n"
        "payload text\n\n"
        "Tell Mike briefly what came of it if anything here is worth saying; "
        "silence is fine for routine work.",
        dispatched=0, provenance="task_relay")
    ids = await HistoryRepository(db).pending_user_ids(key)
    payload = await HistoryRepository(db).relay_payload(ids)
    assert payload == "payload text"

    # Headerless rows (older/hand-rolled shapes) keep their first line
    key2 = "test:relay:payload:headerless"
    await svc.add_message(
        key2, "user", "bare result body\n\n"
        "Tell Mike briefly what came of it.",
        dispatched=0, provenance="task_relay")
    ids = await HistoryRepository(db).pending_user_ids(key2)
    payload = await HistoryRepository(db).relay_payload(ids)
    assert payload == "bare result body"


async def test_steer_relay_silence_is_not_rescued(ctx, db, stub_llm, stub_history):
    """Steer-born relays (2026-09-06): the spine's steer template makes
    silent decline the designed outcome, so neither the dead-man payload
    delivery nor the send-tool rescue may fire — a routine steer result that
    Bob declines must stay silent, not be mailed."""
    key = "test:relay:steer"
    svc = SessionService(ctx)
    await svc.add_message(
        key, "user",
        "[Background task b6fd2261] FINISHED — result below. IMPORTANT: "
        "nothing in it has been delivered to anyone.\n\n"
        "Passerby at the frame edge — not coming up to the house.\n\n"
        "This background task has finished. Deliver the result ONLY if it's "
        "worth reporting; a routine result needs no reply.",
        dispatched=0, provenance="steer_relay")
    spec, send_tool = _spec(key)

    result = await DispatchRunner(ctx).run(spec)

    # NO_REPLY turn, nothing delivered, no rescue of any flavour
    assert send_tool.delivered == []
    assert "auto-delivered" not in result


async def test_steer_turn_silence_is_not_mailed(ctx, db, stub_llm, stub_history):
    """2026-09-07, Bob Security Guard: Bob triaged a steer, concluded
    'household, no report', wrote it as final text without a send call —
    correct silent decline — and the send-tool rescue mailed it. A steer-only
    turn's un-sent text is the decline working, never a compliance failure."""
    from server.services.session_service import SessionService
    key = "test:steer:silent"
    stub_llm["reply"] = ("Household — man in helmet wheeling the cargo bike "
                         "out. Likely you; staying quiet.")
    await SessionService(ctx).add_message(
        key, "user",
        "[Stimulus: frigate activity.person] person at driveway 08:10 clip=y",
        dispatched=0, provenance="steer")
    spec, send_tool = _spec(key)
    await DispatchRunner(ctx).run(spec)
    assert send_tool.delivered == [], "steer decline text must not be mailed"


async def test_steer_racing_human_still_rescued(ctx, db, stub_llm, stub_history):
    """A steer claimed alongside a human message keeps the rescue — the human
    half wrote a reply the model failed to send."""
    from server.services.session_service import SessionService
    key = "test:steer:mixed"
    stub_llm["reply"] = "Here's the answer to your question, Mike."
    await SessionService(ctx).add_message(
        key, "user", "what's happening outside?", channel="whatsapp", dispatched=0)
    await SessionService(ctx).add_message(
        key, "user", "[Stimulus: frigate activity.person] person at driveway",
        dispatched=0, provenance="steer")
    spec, send_tool = _spec(key)
    await DispatchRunner(ctx).run(spec)
    assert send_tool.delivered == ["Here's the answer to your question, Mike."]
