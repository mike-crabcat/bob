"""Tests for the Realtime bridge voicemail end-watch.

An agent that reaches voicemail, leaves a message, but never calls end_call
leaves the line open in dead air until max_duration (observed 2026-08-17:
299.8s — the call only ended because the carrier's own recording cap beeped).
The bridge detects voicemail-shaped opening turns on outbound calls and ends
the session a short reply-window after the agent's message, unless a human
replies (misdetection self-corrects).
"""

from __future__ import annotations

import asyncio

from bob_server.services.realtime_bridge import RealtimeBridge
from bob_server.services.tools import tool


@tool
async def end_call() -> str:
    """End the call."""


class FakeOai:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        import json
        self.sent.append(json.loads(payload))


class FakeSource:
    async def recv_mic_pcm16_24k(self) -> bytes | None:
        return None

    async def send_speaker_pcm16_24k(self, chunk: bytes) -> None:
        pass

    async def clear_playback(self) -> None:
        pass

    def input_closed(self) -> bool:
        return False

    async def aclose(self) -> None:
        pass


def _bridge(speak_first: bool = False, tools: list | None = None) -> tuple[RealtimeBridge, FakeOai]:
    if tools is None:
        tools = [end_call]
    bridge = RealtimeBridge(
        FakeSource(),
        api_key="k", model="m", instructions="i", voice="v",
        tools=tools,
        speak_first=speak_first,
        opening_listen_seconds=5.0,
    )
    bridge._VOICEMAIL_REPLY_GRACE_SECONDS = 0.05  # compress the reply window
    oai = FakeOai()
    return bridge, oai


async def _greeting(bridge: RealtimeBridge, oai: FakeOai, text: str) -> None:
    await bridge._dispatch_event(oai, "conversation.item.input_audio_transcription.completed", {
        "transcript": text,
    })


async def _agent_message_done(bridge: RealtimeBridge, oai: FakeOai) -> None:
    bridge._current_agent = "Hi, just checking whether your meeting is going okay."
    await bridge._dispatch_event(oai, "response.done", {"response": {"id": "r1"}})


async def test_voicemail_greeting_ends_call_after_reply_window():
    bridge, oai = _bridge()
    await bridge._dispatch_event(oai, "session.updated", {})
    await _greeting(bridge, oai, "Hey, this is Thomas. I'm not here right now. Please leave a message")
    assert bridge._voicemail_detected is True

    await _agent_message_done(bridge, oai)
    assert bridge._voicemail_watch_task is not None

    # Compress the window: run the watch with a short grace directly.
    watch = bridge._voicemail_watch_task
    await asyncio.wait_for(watch, timeout=2.0)
    assert bridge._end_requested.is_set()


async def test_voicemail_detection_without_right_now():
    """Carrier phrasings without "right now" must arm the detector too —
    2026-08-18: "The person you are calling is not available." (Simon's
    greeting) didn't match, so no reply-window watch was armed."""
    bridge, oai = _bridge()
    await bridge._dispatch_event(oai, "session.updated", {})
    await _greeting(bridge, oai, "The person you are calling is not available.")
    assert bridge._voicemail_detected is True
    await _greeting(bridge, oai, "Sorry, Simon isn't available at the moment.")
    assert bridge._voicemail_detected is True


async def test_live_greeting_never_arms_watch():
    bridge, oai = _bridge()
    await bridge._dispatch_event(oai, "session.updated", {})
    await _greeting(bridge, oai, "Hello, Thomas speaking")
    await _agent_message_done(bridge, oai)
    assert bridge._voicemail_detected is False
    assert bridge._voicemail_watch_task is None
    assert not bridge._end_requested.is_set()


async def test_human_reply_inside_window_disarms_end():
    """Misdetection — a live human offering to take a message — self-corrects:
    their reply inside the window stops the forced end."""
    bridge, oai = _bridge()
    await bridge._dispatch_event(oai, "session.updated", {})
    await _greeting(bridge, oai, "Thomas can't come to the phone right now, can I take a message?")
    assert bridge._voicemail_detected is True

    await _agent_message_done(bridge, oai)
    # The human answers before the window closes.
    await _greeting(bridge, oai, "Yep no worries, I'll pass it on")

    task = bridge._voicemail_watch_task
    assert task is not None
    await asyncio.wait_for(task, timeout=2.0)
    assert not bridge._end_requested.is_set()


async def test_model_ending_call_itself_never_arms_watch():
    bridge, oai = _bridge()
    await bridge._dispatch_event(oai, "session.updated", {})
    await _greeting(bridge, oai, "Please leave a message after the beep")
    await bridge.request_end()  # the model called end_call with its message
    bridge._current_agent = "message left"
    await bridge._dispatch_event(oai, "response.done", {"response": {"id": "r1"}})
    assert bridge._voicemail_watch_task is None


async def test_inbound_session_never_arms_watch():
    """speak_first (inbound/test) can't hit voicemail — never force-ended."""
    bridge, oai = _bridge(speak_first=True)
    await bridge._dispatch_event(oai, "session.updated", {})
    await _greeting(bridge, oai, "Leave a message after the tone")
    await _agent_message_done(bridge, oai)
    assert bridge._voicemail_detected is False
    assert bridge._voicemail_watch_task is None


async def test_session_without_end_call_never_arms_watch():
    """Voice-link sessions strip end_call (the human hangs up) — the bridge
    must not force them closed."""
    bridge, oai = _bridge(tools=[])
    await bridge._dispatch_event(oai, "session.updated", {})
    await _greeting(bridge, oai, "Leave a message after the tone")
    await _agent_message_done(bridge, oai)
    assert bridge._voicemail_detected is False
    assert bridge._voicemail_watch_task is None


async def test_reverse_order_transcript_after_response_still_arms():
    """Greeting transcript landing AFTER the agent's message still arms the
    watch (the other arrival order)."""
    bridge, oai = _bridge()
    await bridge._dispatch_event(oai, "session.updated", {})
    await _agent_message_done(bridge, oai)
    await _greeting(bridge, oai, "I can't come to the phone right now, leave a message")
    assert bridge._voicemail_detected is True
    assert bridge._voicemail_watch_task is not None
    await asyncio.wait_for(bridge._voicemail_watch_task, timeout=2.0)
    assert bridge._end_requested.is_set()
