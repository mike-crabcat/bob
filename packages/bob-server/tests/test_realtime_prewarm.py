"""Tests for prewarmed Realtime sessions (outbound phone calls).

The OpenAI session is connected + configured while the phone RINGS, then
claimed by the media handler at answer — the callee's greeting must flow
into a live, fully-configured session at 1× instead of riding a
setup-backlog burst into a half-configured one (2026-08-17 greeting misses).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from bob_server.services import realtime_prewarm
from bob_server.services import realtime_bridge as bridge_mod
from bob_server.services.realtime_bridge import RealtimeBridge


class FakeWs:
    """Minimal websockets client: send() records, ``incoming`` drives reads."""

    def __init__(self, incoming: list[dict] | None = None) -> None:
        self.sent: list[dict] = []
        self.incoming = [json.dumps(e) for e in (incoming or [])]
        self.closed = False
        self.pings = 0

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def close(self) -> None:
        self.closed = True

    async def ping(self):
        if self.closed:
            raise RuntimeError("closed")
        self.pings += 1
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
    """AudioSource stand-in: no mic input, accepts speaker chunks."""

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


def _bridge(**kw) -> RealtimeBridge:
    return RealtimeBridge(
        None,  # audio attached when the media stream claims the session
        api_key="k", model="m", instructions="i", voice="v", tools=[],
        **kw,
    )


def _patch_connect(monkeypatch, ws: FakeWs) -> None:
    async def fake_connect(uri, additional_headers=None):
        return ws
    monkeypatch.setattr(bridge_mod.websockets, "connect", fake_connect)


def _settings() -> SimpleNamespace:
    rt = SimpleNamespace(
        model="m", voice="v", max_call_duration_seconds=300,
        turn_detection="server_vad",
    )
    return SimpleNamespace(openai_realtime=rt, openai=SimpleNamespace(api_key="k"))


async def test_prewarm_connects_and_waits_for_session_updated(monkeypatch):
    ws = FakeWs(incoming=[{"type": "session.updated"}])
    _patch_connect(monkeypatch, ws)

    bridge = _bridge()
    await bridge.prewarm()

    assert bridge._prewarmed_ws is ws
    assert bridge._session_ready.is_set()
    assert any(m.get("type") == "session.update" for m in ws.sent)
    assert bridge._prewarmed_session_updated == {"type": "session.updated"}
    assert not ws.closed  # stays live for the media handler to claim


async def test_prewarm_error_abandons_socket(monkeypatch):
    ws = FakeWs(incoming=[{"type": "error", "error": {"message": "bad session"}}])
    _patch_connect(monkeypatch, ws)

    bridge = _bridge()
    with pytest.raises(RuntimeError):
        await bridge.prewarm()

    assert ws.closed
    assert bridge._prewarmed_ws is None


async def test_run_with_prewarmed_session_arms_gate_at_answer(monkeypatch):
    """The stashed session.updated replays through normal dispatch when the
    media stream attaches — the opening gate window starts at ANSWER time."""
    ws = FakeWs(incoming=[{"type": "session.updated"}])
    _patch_connect(monkeypatch, ws)

    bridge = _bridge(speak_first=False)  # outbound callee-first convention
    await bridge.prewarm()
    bridge.audio = FakeSource()

    assert bridge._grace_until == 0  # not armed during ringing
    result = await bridge.run()

    assert bridge._grace_until > 0  # armed at claim
    assert ws.closed  # run owns and closes the claimed socket
    assert result.end_reason == "completed"
    assert result.duration_seconds > 0


async def test_run_with_prewarmed_speak_first_opens_at_answer(monkeypatch):
    ws = FakeWs(incoming=[{"type": "session.updated"}])
    _patch_connect(monkeypatch, ws)

    bridge = _bridge(speak_first=True)
    await bridge.prewarm()
    assert not any(m.get("type") == "response.create" for m in ws.sent)  # silent while ringing

    bridge.audio = FakeSource()
    await bridge.run()
    assert any(m.get("type") == "response.create" for m in ws.sent)  # opens at answer


async def test_registry_roundtrip_is_one_shot(monkeypatch):
    ws = FakeWs(incoming=[{"type": "session.updated"}])
    _patch_connect(monkeypatch, ws)

    realtime_prewarm.start_prewarm(
        "call-1", db=None, settings=_settings(),
        phone_number="+61000000000", agenda="test the prewarm", meta={},
    )
    claimed = await realtime_prewarm.claim("call-1")
    assert claimed is not None
    assert claimed._prewarmed_ws is ws
    # One-shot: a Twilio stream reconnect falls back to connect-at-answer.
    assert await realtime_prewarm.claim("call-1") is None
    await claimed.abandon()


async def test_claim_of_failed_prewarm_falls_back(monkeypatch):
    ws = FakeWs(incoming=[{"type": "error", "error": {}}])
    _patch_connect(monkeypatch, ws)

    realtime_prewarm.start_prewarm(
        "call-2", db=None, settings=_settings(),
        phone_number="+61000000000", agenda="", meta={},
    )
    assert await realtime_prewarm.claim("call-2") is None
    assert ws.closed


async def test_discard_closes_unclaimed_session(monkeypatch):
    ws = FakeWs(incoming=[{"type": "session.updated"}])
    _patch_connect(monkeypatch, ws)

    realtime_prewarm.start_prewarm(
        "call-3", db=None, settings=_settings(),
        phone_number="+61000000000", agenda="", meta={},
    )
    await asyncio.sleep(0.05)  # let prewarm complete
    await realtime_prewarm.discard("call-3")

    assert ws.closed
    assert await realtime_prewarm.claim("call-3") is None


async def test_ttl_expiry_discards_unclaimed_session(monkeypatch):
    ws = FakeWs(incoming=[{"type": "session.updated"}])
    _patch_connect(monkeypatch, ws)
    monkeypatch.setattr(realtime_prewarm, "_PREWARM_TTL_SECONDS", 0.05)

    realtime_prewarm.start_prewarm(
        "call-4", db=None, settings=_settings(),
        phone_number="+61000000000", agenda="", meta={},
    )
    await asyncio.sleep(0.2)  # TTL passes
    assert ws.closed
    assert await realtime_prewarm.claim("call-4") is None
