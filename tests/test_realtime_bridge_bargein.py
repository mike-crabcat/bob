"""Tests for echo-safe barge-in and session-task cleanup in the Realtime bridge.

Barge-in (2026-08-22): clear_playback used to fire on bare
input_audio_buffer.speech_started. An analog landline echoes our own outbound
audio back, VAD commits the echo just like speech, and the agent cut its OWN
sentence mid-word — both calls to an echoey venue collapsed this way. Echo
produces VAD energy but never a transcription, so the interrupt is deferred to
a non-empty user transcript instead.

Cleanup (2026-08-22): when the bridge task is cancelled (callee hung up),
asyncio.wait inside _run_session raises instead of returning, so the
pending-task cancel loop never ran — the duration timer slept out its full
window ("max duration reached" minutes after the call ended) and the
end_requested waiter never completed.
"""

from __future__ import annotations

import asyncio
import logging

from bob_server.services.realtime_bridge import RealtimeBridge


class FakeOai:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        import json
        self.sent.append(json.loads(payload))

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        # No events ever arrive — the event loop task must block, as it would
        # against a live OpenAI websocket.
        await asyncio.Event().wait()
        raise StopAsyncIteration


class TrackingSource:
    """AudioSource that records the ORDER of plays vs. clears — barge-in
    correctness is exactly that ordering."""

    def __init__(self, mic_chunks: int = 0, block_after_drain: bool = True) -> None:
        self.log: list[str] = []
        self.played: list[bytes] = []
        self._mic_chunks = [b"\x01\x00"] * mic_chunks
        self._block_after_drain = block_after_drain

    async def recv_mic_pcm16_24k(self) -> bytes | None:
        if self._mic_chunks:
            return self._mic_chunks.pop(0)
        if self._block_after_drain:
            await asyncio.Event().wait()  # mic still open, no audio
        return None  # input closed — relay ends

    async def send_speaker_pcm16_24k(self, chunk: bytes) -> None:
        self.log.append("play")
        self.played.append(chunk)

    async def clear_playback(self) -> None:
        self.log.append("clear")

    def input_closed(self) -> bool:
        return False

    async def aclose(self) -> None:
        pass


def _bridge(source: TrackingSource, **kw) -> RealtimeBridge:
    return RealtimeBridge(
        source,
        api_key="k", model="m", instructions="i", voice="v",
        speak_first=False,
        opening_listen_seconds=5.0,
        **kw,
    )


async def test_speech_started_alone_does_not_clear_playback():
    """VAD firing mid-playback is NOT a barge-in — on an echoey line it is our
    own voice coming back. Playback must survive it."""
    source = TrackingSource()
    bridge = _bridge(source)
    oai = FakeOai()

    await bridge._dispatch_event(oai, "session.updated", {})
    # A real mid-call turn: user spoke, agent is responding, audio is queued.
    await bridge._dispatch_event(oai, "conversation.item.input_audio_transcription.completed", {
        "transcript": "hello?",
    })
    await bridge._dispatch_event(oai, "response.created", {"response": {"id": "r1"}})
    await bridge._dispatch_event(oai, "response.output_audio.delta", {
        "response_id": "r1", "delta": "AAAA",
    })
    assert source.played  # agent audio flowing

    source.log.clear()
    await bridge._dispatch_event(oai, "input_audio_buffer.speech_started", {})
    assert source.log == []  # echo (or a fast human) must not cut the agent


async def test_confirmed_user_transcript_cuts_remaining_playback():
    """A non-empty transcript behind the interrupt IS a human barge-in: drop
    whatever of the agent's turn is still queued/playing."""
    source = TrackingSource()
    bridge = _bridge(source)
    oai = FakeOai()

    await bridge._dispatch_event(oai, "session.updated", {})
    await bridge._dispatch_event(oai, "conversation.item.input_audio_transcription.completed", {
        "transcript": "hello?",
    })
    await bridge._dispatch_event(oai, "response.created", {"response": {"id": "r1"}})
    await bridge._dispatch_event(oai, "response.output_audio.delta", {
        "response_id": "r1", "delta": "AAAA",
    })

    source.log.clear()
    await bridge._dispatch_event(oai, "input_audio_buffer.speech_started", {})
    await bridge._dispatch_event(oai, "conversation.item.input_audio_transcription.completed", {
        "transcript": "wait, actually—",
    })
    assert "clear" in source.log  # the real interrupt cut the agent's turn


async def test_empty_transcript_does_not_cut_playback():
    """Echo/noise resolves as an empty transcription — the agent keeps talking."""
    source = TrackingSource()
    bridge = _bridge(source)
    oai = FakeOai()

    await bridge._dispatch_event(oai, "session.updated", {})
    await bridge._dispatch_event(oai, "conversation.item.input_audio_transcription.completed", {
        "transcript": "hello?",
    })
    await bridge._dispatch_event(oai, "response.created", {"response": {"id": "r1"}})
    await bridge._dispatch_event(oai, "response.output_audio.delta", {
        "response_id": "r1", "delta": "AAAA",
    })
    source.log.clear()

    await bridge._dispatch_event(oai, "input_audio_buffer.speech_started", {})
    await bridge._dispatch_event(oai, "conversation.item.input_audio_transcription.completed", {
        "transcript": "",
    })
    assert "clear" not in source.log


async def test_greeting_release_still_plays_held_audio():
    """The clear that fires with the greeting's transcript must not nuke the
    opening-gate release: held audio is only queued AFTER it, so the greeting
    reply still reaches the caller."""
    source = TrackingSource()
    bridge = _bridge(source)
    oai = FakeOai()

    await bridge._dispatch_event(oai, "session.updated", {})
    await bridge._dispatch_event(oai, "response.created", {"response": {"id": "r1"}})
    await bridge._dispatch_event(oai, "response.output_audio.delta", {
        "response_id": "r1", "delta": "AAAA",
    })
    assert source.played == []  # held by the opening gate

    await bridge._dispatch_event(oai, "conversation.item.input_audio_transcription.completed", {
        "transcript": "hello?",
    })
    assert source.played == [b"\x00\x00\x00"]  # released and delivered
    assert source.log == ["clear", "play"]     # clear first — a no-op then


async def test_cancelled_run_session_unwinds_child_tasks(caplog):
    """Cancelling the bridge task (callee hangup) must cancel the duration
    timer instead of letting it sleep out its window."""
    source = TrackingSource()
    bridge = _bridge(source, max_duration_seconds=0.05)

    task = asyncio.create_task(bridge._run_session(FakeOai()))
    await asyncio.sleep(0.01)  # let the session tasks spin up
    task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
    except asyncio.CancelledError:
        pass
    assert task.cancelled()  # the cancellation propagated through _run_session

    with caplog.at_level(logging.INFO, logger="bob_server.services.realtime_bridge"):
        await asyncio.sleep(0.15)  # past the 0.05s duration window
    assert not any("max duration" in r.message for r in caplog.records)


async def test_relay_end_completes_session_normally(caplog):
    """The normal end path (mic relay ended — Twilio stop) still unwinds the
    timer and logs the session-ending line."""
    source = TrackingSource(mic_chunks=1, block_after_drain=False)  # relay ends
    bridge = _bridge(source, max_duration_seconds=0.05)

    with caplog.at_level(logging.INFO, logger="bob_server.services.realtime_bridge"):
        await asyncio.wait_for(bridge._run_session(FakeOai()), timeout=5.0)

    assert any("Bridge session ending" in r.message for r in caplog.records)
    assert not any("max duration" in r.message for r in caplog.records)
