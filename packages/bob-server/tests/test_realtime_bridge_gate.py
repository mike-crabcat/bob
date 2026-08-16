"""Tests for the Realtime bridge opening gate (callee-first call opening).

A noise burst at connect can make server VAD create a response with no
transcribed human speech behind it (2026-08-15: agent opened with "Hi
Sophia" on a call to Ryan). The gate cancels such responses during the
opening window and suppresses their audio + transcript entirely.
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
    await bridge._dispatch_event(oai, "response.created", {"response": {"id": "resp_noise"}})
    assert {"type": "response.cancel"} in oai.sent

    # Its audio and transcript deltas must never reach the caller or the record.
    await bridge._dispatch_event(oai, "response.output_audio.delta", {
        "response_id": "resp_noise", "delta": "AAAA",
    })
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
