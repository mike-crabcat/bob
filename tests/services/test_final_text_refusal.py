"""Final-text delivery must read the send tool's answer (2026-10-15 Bob cast).

The budget wrap-up round (tools stripped) wrote its ffmpeg stitch as
<tool_call> XML; the WhatsApp send tool correctly refused markup-only text
by RETURNING "Error: not sent …", and the runner logged "delivered" — the
group heard nothing. Now: markup-only final text becomes an honest
unfinished-turn notice, a refused send gets that notice once, and a
duplicate-blocked refusal (the answer already went out) stays silent.
"""

from __future__ import annotations

from server.services.dispatch_runner import (
    _UNFINISHED_TURN_NOTICE,
    DispatchRunner,
    _send_refusal,
)
from server.services.session_service import SessionService
from tests.services.test_dispatch_rescue_gating import (  # noqa: F401  (fixtures)
    _FakeSendTool,
    _spec,
    stub_history,
    stub_llm,
)

MARKUP = ("<tool_call>bash<arg_key>command</arg_key><arg_value>cd scratch/bobcast "
          "&& ffmpeg -y … bobcast-news-ep1.mp3</arg_value></tool_call>")


class _RefusingSendTool(_FakeSendTool):
    """Refuses the first send with the given error, then accepts."""

    def __init__(self, name: str, error: str):
        super().__init__(name)
        self.error = error
        self.attempts: list[str] = []

    async def handler(self, text: str) -> str:
        self.attempts.append(text)
        if len(self.attempts) == 1:
            return self.error
        return await super().handler(text)


def test_send_refusal_reads_error_strings_only():
    assert _send_refusal("Error: not sent — x") == "Error: not sent — x"
    assert _send_refusal('{"ok": true}') is None
    assert _send_refusal("sent") is None
    assert _send_refusal(None) is None


async def test_markup_only_final_text_becomes_unfinished_notice(
        ctx, db, stub_llm, stub_history):
    key = "test:final:markup"
    await SessionService(ctx).add_message(key, "user", "make the episode", dispatched=0)
    stub_llm["reply"] = MARKUP
    send_tool = _FakeSendTool("send_whatsapp_message")

    await DispatchRunner(ctx).run(_spec(key, send_tool))

    assert send_tool.delivered == [_UNFINISHED_TURN_NOTICE]


async def test_refused_send_falls_back_to_notice_once(
        ctx, db, stub_llm, stub_history):
    key = "test:final:refused"
    await SessionService(ctx).add_message(key, "user", "hello", dispatched=0)
    stub_llm["reply"] = "here's the episode"
    send_tool = _RefusingSendTool("send_whatsapp_message",
                                  "Error sending message: bridge down")

    await DispatchRunner(ctx).run(_spec(key, send_tool))

    assert send_tool.attempts == ["here's the episode", _UNFINISHED_TURN_NOTICE]
    assert send_tool.delivered == [_UNFINISHED_TURN_NOTICE]


async def test_duplicate_blocked_refusal_sends_nothing_more(
        ctx, db, stub_llm, stub_history):
    key = "test:final:dup"
    await SessionService(ctx).add_message(key, "user", "hello", dispatched=0)
    stub_llm["reply"] = "already said this"
    send_tool = _RefusingSendTool(
        "send_whatsapp_message",
        "Error: not sent — duplicate blocked: this exact text was just sent")

    await DispatchRunner(ctx).run(_spec(key, send_tool))

    assert send_tool.attempts == ["already said this"]
    assert send_tool.delivered == []


async def test_normal_reply_unchanged(ctx, db, stub_llm, stub_history):
    key = "test:final:normal"
    await SessionService(ctx).add_message(key, "user", "hello", dispatched=0)
    stub_llm["reply"] = "hi mike"
    send_tool = _FakeSendTool("send_whatsapp_message")

    await DispatchRunner(ctx).run(_spec(key, send_tool))

    assert send_tool.delivered == ["hi mike"]
