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


# ------------------------------------------------------------ relay content

def test_relay_content_states_delivery_truth():
    content = BackburnerService._relay_content(
        "abcd1234", "Reply sent to Andrew with the sources.", failed=False)
    assert "nothing in it has been delivered" in content
    assert "'sent'" in content, "must defuse the captured-send claim"
    assert "NO_REPLY" in content, "must name the loss"
    # The boilerplate tail relay_payload splits on must be present verbatim.
    assert "\n\nThis background task has finished." in content
    assert content.index("nothing in it has been delivered") < content.index(
        "Reply sent to Andrew"), "truth leads, payload follows"


def test_relay_content_failed_variant():
    content = BackburnerService._relay_content("abcd1234", "it broke", failed=True)
    assert "NO_REPLY" in content
    assert "\n\nThis background task failed." in content


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
    """The 2026-09-03 incident: the relay turn answers NO_REPLY believing
    the result already went out. The runner delivers the payload itself."""
    key = "test:relay:deadman"
    svc = SessionService(ctx)
    await svc.add_message(
        key, "user",
        "[Background task abcd1234] FINISHED — result below. IMPORTANT: "
        "nothing in it has been delivered to anyone.\n\n"
        "Reply sent to Andrew with the deep-dive sources: METR report et al.\n\n"
        "This background task has finished. Relay the result to the user now "
        "with a short summary in your own voice — call your send tool with it; "
        "replying NO_REPLY here loses the result entirely.",
        dispatched=0, provenance="task_relay")
    spec, send_tool = _spec(key)

    await DispatchRunner(ctx).run(spec)

    assert send_tool.delivered == [
        "[Background task abcd1234] FINISHED — result below. IMPORTANT: "
        "nothing in it has been delivered to anyone.\n\n"
        "Reply sent to Andrew with the deep-dive sources: METR report et al."]


async def test_relay_delivered_turn_no_dead_man(
        ctx, db, stub_llm, stub_history):
    """A relay turn that speaks (via the normal rescue) must not ALSO get
    the dead-man payload — one delivery, not two."""
    key = "test:relay:spoken"
    stub_llm["reply"] = "Andrew — here are the sources you wanted."
    svc = SessionService(ctx)
    await svc.add_message(
        key, "user",
        "[Background task abcd1234] result body\n\n"
        "This background task has finished. Relay the result to the user.",
        dispatched=0, provenance="task_relay")
    spec, send_tool = _spec(key)

    await DispatchRunner(ctx).run(spec)

    assert send_tool.delivered == ["Andrew — here are the sources you wanted."]


async def test_relay_payload_strips_boilerplate_and_caps(ctx, db):
    from server.repositories.history import HistoryRepository
    key = "test:relay:payload"
    svc = SessionService(ctx)
    await svc.add_message(
        key, "user", "[Background task abcd1234] payload text\n\n"
        "This background task has finished. Relay the result.",
        dispatched=0, provenance="task_relay")
    ids = await HistoryRepository(db).pending_user_ids(key)
    payload = await HistoryRepository(db).relay_payload(ids)
    assert payload == "[Background task abcd1234] payload text"
