"""Tests for the end-of-call grace drain.

When the agent calls end_call, two things are still in flight: the caller's
final utterance transcription (whisper races the tool event) and the tail of
the agent's own queued audio. The bridge holds the ended session open for a
short bounded window so both land (2026-08-20: David's audible "bye" was
dropped from the transcript; earlier, hangups could cut the agent's goodbye
audio mid-word).
"""

from __future__ import annotations

import asyncio
import json

from bob_server.services.realtime_bridge import RealtimeBridge


class FakeWs:
    """Minimal websocket: send() records, ``incoming`` drives reads."""

    def __init__(self, incoming: list[dict]) -> None:
        self.sent: list[dict] = []
        self.incoming = [json.dumps(e) for e in incoming]
        self.closed = False

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def close(self) -> None:
        self.closed = True

    async def ping(self):
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(None)
        return fut

    def __aiter__(self) -> "FakeWs":
        return self

    async def __anext__(self) -> str:
        if not self.incoming or self.closed:
            raise StopAsyncIteration
        return self.incoming.pop(0)


class FakeSource:
    def __init__(self) -> None:
        self.played: list[bytes] = []

    async def recv_mic_pcm16_24k(self) -> bytes | None:
        return None

    async def send_speaker_pcm16_24k(self, chunk: bytes) -> None:
        self.played.append(chunk)

    async def clear_playback(self) -> None:
        pass

    def input_closed(self) -> bool:
        return False

    async def aclose(self) -> None:
        pass


def _end_call_event() -> dict:
    return {
        "type": "response.function_call_arguments.done",
        "call_id": "c1",
        "name": "end_call",
        "arguments": "{}",
    }


def _bridge() -> RealtimeBridge:
    return RealtimeBridge(
        FakeSource(), api_key="k", model="m", instructions="i", voice="v",
        tools=[], speak_first=False,
    )


async def test_transcription_after_end_request_still_recorded():
    """The caller's goodbye transcription arrives AFTER the end_call tool
    event — the event loop must keep dispatching, not stop at end_requested."""
    bridge = _bridge()
    ws = FakeWs([
        _end_call_event(),
        {"type": "conversation.item.input_audio_transcription.completed",
         "transcript": "Okay Bob, bye!"},
    ])

    await bridge._event_loop(ws)

    assert bridge._end_requested.is_set()
    assert bridge._turns == ["User: Okay Bob, bye!"]


async def test_event_loop_without_end_keeps_running():
    bridge = _bridge()
    ws = FakeWs([
        {"type": "conversation.item.input_audio_transcription.completed",
         "transcript": "hello"},
    ])
    await bridge._event_loop(ws)
    assert bridge._turns == ["User: hello"]
    assert not bridge._end_requested.is_set()


async def test_grace_drain_waits_for_min_window():
    """A pending events task is given at least the minimum drain window."""
    bridge = _bridge()
    bridge._END_DRAIN_MIN_SECONDS = 0.05
    bridge._END_DRAIN_MAX_SECONDS = 0.1

    async def never():
        await asyncio.Event().wait()

    events = asyncio.create_task(never())
    try:
        await asyncio.wait_for(
            asyncio.shield(bridge._end_grace_drain(events)), timeout=1.0,
        )
    finally:
        events.cancel()


async def test_grace_drain_returns_early_when_events_done():
    """If the events loop already finished, the drain must not linger."""
    bridge = _bridge()
    bridge._END_DRAIN_MIN_SECONDS = 5.0  # would stall the test if honored blindly
    events = asyncio.create_task(asyncio.sleep(0))

    import time
    t0 = time.monotonic()
    await bridge._end_grace_drain(events)
    assert time.monotonic() - t0 < 1.0


async def test_playback_tail_extends_drain_deadline():
    """Queued goodbye audio holds the drain open past the minimum window."""
    bridge = _bridge()
    bridge._END_DRAIN_MIN_SECONDS = 0.01
    bridge._END_DRAIN_MAX_SECONDS = 5.0
    bridge._note_playback(b"\x00" * 48000)  # 1.0s of PCM16 mono @24k
    assert bridge._last_audio_queued_dur == 1.0

    async def never():
        await asyncio.Event().wait()

    events = asyncio.create_task(never())
    task = asyncio.create_task(bridge._end_grace_drain(events))
    try:
        await asyncio.sleep(0.08)  # past the minimum window
        assert not task.done()  # audio tail (~1.4s) still holds it open
    finally:
        events.cancel()
        task.cancel()
