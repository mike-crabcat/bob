"""Tests for the Realtime bridge opening gate (callee-first call opening).

A noise burst at connect can make server VAD create a response with no
transcribed human speech behind it (2026-08-15: agent opened with "Hi
Sophia" on a call to Ryan). But the greeting's input transcription RACES
response.created and can lose (2026-08-17: a real "hello?" was cancelled as
noise and the caller sat through the whole opening window in dead air). So
the gate HOLDS a transcript-less response created during the opening window:
its output is buffered until a human transcript lands (release — play and
record normally) or the decision window closes with none (cancel and
suppress, as before).
"""

from __future__ import annotations

import asyncio

from bob_server.services.realtime_bridge import RealtimeBridge


class FakeOai:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        import json
        self.sent.append(json.loads(payload))


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


def _bridge(speak_first: bool = False) -> tuple[RealtimeBridge, FakeOai, FakeSource]:
    source = FakeSource()
    bridge = RealtimeBridge(
        source,
        api_key="k", model="m", instructions="i", voice="v",
        speak_first=speak_first,
        opening_listen_seconds=5.0,
    )
    oai = FakeOai()
    return bridge, oai, source


async def test_noise_response_during_opening_is_cancelled_and_suppressed():
    bridge, oai, source = _bridge()

    await bridge._dispatch_event(oai, "session.updated", {})  # arms the gate
    assert bridge._grace_until > 0

    # VAD fires on connection noise: response created, no user transcript yet.
    # It is held, not instantly cancelled — the transcript may still land.
    await bridge._dispatch_event(oai, "response.created", {"response": {"id": "resp_noise"}})
    assert not any(m.get("type") == "response.cancel" for m in oai.sent)

    # While the verdict is pending, its audio reaches the caller nowhere.
    await bridge._dispatch_event(oai, "response.output_audio.delta", {
        "response_id": "resp_noise", "delta": "AAAA",
    })
    assert source.played == []

    # Decision window closes with no transcription behind it: noise.
    bridge.gate_decision_seconds = 0
    await bridge._gate_decide(oai)
    assert {"type": "response.cancel"} in oai.sent

    # Its transcript deltas must never reach the record either.
    await bridge._dispatch_event(oai, "response.output_audio_transcript.delta", {
        "response_id": "resp_noise", "delta": "Hi Sophia!",
    })
    assert source.played == []
    assert bridge._current_agent == ""

    await bridge._dispatch_event(oai, "response.done", {"response": {"id": "resp_noise"}})
    assert bridge._turns == []  # suppressed turn leaves no transcript record
    assert bridge._conversational_response_done is False
    assert not bridge._response_in_flight
    # Gate state lets the nudge prompt a clean opening turn after the window.
    assert bridge._suppressed_response_ids == set()


async def test_greeting_transcript_landing_after_response_releases_held_audio():
    """2026-08-17 live-call ordering: response.created beat the greeting's
    transcription — the gate must hold, then release, not cancel."""
    bridge, oai, source = _bridge()

    await bridge._dispatch_event(oai, "session.updated", {})
    await bridge._dispatch_event(oai, "response.created", {"response": {"id": "resp_ok"}})

    # Agent audio generates before the greeting transcript lands: held.
    await bridge._dispatch_event(oai, "response.output_audio.delta", {
        "response_id": "resp_ok", "delta": "AAAA",
    })
    assert source.played == []

    # The greeting's transcription finally lands -> release.
    await bridge._dispatch_event(oai, "conversation.item.input_audio_transcription.completed", {
        "transcript": "hello?",
    })
    assert source.played == [b"\x00\x00\x00"]  # held audio flushed to the caller
    assert not any(m.get("type") == "response.cancel" for m in oai.sent)

    # Live deltas flow normally after the release.
    await bridge._dispatch_event(oai, "response.output_audio.delta", {
        "response_id": "resp_ok", "delta": "BBBB",
    })
    assert source.played == [b"\x00\x00\x00", b"\x04\x10A"]
    assert bridge._user_transcript_seen


async def test_held_response_done_then_transcript_completes_turn():
    """response.done can arrive while still held — its bookkeeping must run
    when the transcript confirms the turn was real."""
    bridge, oai, source = _bridge()

    await bridge._dispatch_event(oai, "session.updated", {})
    await bridge._dispatch_event(oai, "response.created", {"response": {"id": "r1"}})
    await bridge._dispatch_event(oai, "response.output_audio_transcript.delta", {
        "response_id": "r1", "delta": "Hi there!",
    })
    assert bridge._current_agent == ""  # held, not recorded yet
    await bridge._dispatch_event(oai, "response.done", {"response": {"id": "r1"}})
    assert bridge._conversational_response_done is False  # deferred

    await bridge._dispatch_event(oai, "conversation.item.input_audio_transcription.completed", {
        "transcript": "hello",
    })
    assert bridge._turns == ["User: hello", "Agent: Hi there!"]
    assert bridge._conversational_response_done is True
    assert bridge._current_agent == ""


async def test_empty_transcription_behind_held_response_is_noise():
    """An empty transcription verdict decides the hold early: cancel."""
    bridge, oai, source = _bridge()

    await bridge._dispatch_event(oai, "session.updated", {})
    await bridge._dispatch_event(oai, "response.created", {"response": {"id": "r1"}})
    await bridge._dispatch_event(oai, "response.output_audio.delta", {
        "response_id": "r1", "delta": "AAAA",
    })
    await bridge._dispatch_event(oai, "conversation.item.input_audio_transcription.completed", {
        "transcript": "",
    })
    assert {"type": "response.cancel"} in oai.sent
    assert source.played == []
    assert not bridge._gate_held


async def test_real_greeting_response_is_not_cancelled():
    bridge, oai, source = _bridge()

    await bridge._dispatch_event(oai, "session.updated", {})
    # Callee speaks; transcription lands before the response is created.
    await bridge._dispatch_event(oai, "conversation.item.input_audio_transcription.completed", {
        "transcript": "hello?",
    })
    await bridge._dispatch_event(oai, "response.created", {"response": {"id": "resp_ok"}})

    assert not any(m.get("type") == "response.cancel" for m in oai.sent)
    await bridge._dispatch_event(oai, "response.output_audio.delta", {
        "response_id": "resp_ok", "delta": "AAAA",
    })
    assert len(source.played) == 1  # audio flows


async def test_noise_after_transcript_is_not_cancelled():
    """Once real speech is seen, late-window responses pass (mid-call turns)."""
    bridge, oai, source = _bridge()

    await bridge._dispatch_event(oai, "session.updated", {})
    await bridge._dispatch_event(oai, "conversation.item.input_audio_transcription.completed", {
        "transcript": "hello",
    })
    await bridge._dispatch_event(oai, "response.done", {"response": {"id": "r1"}})
    # Still within the 5s window, but a conversational turn already happened.
    await bridge._dispatch_event(oai, "response.created", {"response": {"id": "r2"}})
    assert not any(m.get("type") == "response.cancel" for m in oai.sent)


async def test_speak_first_mode_has_no_gate():
    bridge, oai, source = _bridge(speak_first=True)
    await bridge._dispatch_event(oai, "session.updated", {})
    assert bridge._grace_until == 0
    assert {"type": "response.create"} in oai.sent  # agent opens immediately
    # And a response created right away is not treated as noise.
    await bridge._dispatch_event(oai, "response.created", {"response": {"id": "r"}})
    assert not any(m.get("type") == "response.cancel" for m in oai.sent)


async def test_opening_gate_nudge_prompts_after_silent_window():
    bridge, oai, source = _bridge()
    await bridge._dispatch_event(oai, "session.updated", {})
    # Silence for the whole window: nudge (run with compressed timing).
    bridge.opening_listen_seconds = 0.01
    await bridge._first_response_nudge(oai)
    assert {"type": "response.create"} in oai.sent


async def test_monologue_guard_tightens_instructions_after_long_turn():
    """A too-long assistant turn re-sends session.update with a reminder appended,
    capped at two nudges; a short turn never fires it."""
    bridge, oai, source = _bridge()
    await bridge._dispatch_event(oai, "session.updated", {})
    oai.sent.clear()

    # Short turn: no nudge.
    bridge._current_agent = "Hi, do you have any in stock?"
    await bridge._dispatch_event(oai, "response.done", {"response": {"id": "r1"}})
    assert bridge._monologue_nudges == 0
    assert not any(p.get("type") == "session.update" for p in oai.sent)

    # Monologue: nudge 1 — session.update resent with the reminder.
    long_turn = (
        "Hi, I'm Bob, calling on behalf of Mike with a stock question. Could you tell me "
        "if you have any Sega Mega Drive II consoles in stock, including anything not "
        "listed online, like old stock, returns, display units, or items elsewhere in "
        "your store network? And just to be clear, he's after the original-style console, "
        "not a modern mini version, unless that is all you have."
    )
    bridge._current_agent = long_turn
    await bridge._dispatch_event(oai, "response.done", {"response": {"id": "r2"}})
    assert bridge._monologue_nudges == 1
    updates = [p for p in oai.sent if p.get("type") == "session.update"]
    assert updates and "too long for a phone call" in updates[-1]["session"]["instructions"]

    # Cap: after two nudges, instructions stop growing.
    bridge._current_agent = long_turn
    await bridge._dispatch_event(oai, "response.done", {"response": {"id": "r3"}})
    bridge._current_agent = long_turn
    await bridge._dispatch_event(oai, "response.done", {"response": {"id": "r4"}})
    assert bridge._monologue_nudges == 2
    final = [p for p in oai.sent if p.get("type") == "session.update"][-1]["session"]["instructions"]
    assert final.count("too long for a phone call") == 2
