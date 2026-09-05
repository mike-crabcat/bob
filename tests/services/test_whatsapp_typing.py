"""Typing-indicator (outbound chat presence) tests.

The indicator is a cosmetic side-channel: while a WhatsApp dispatch turn
runs, the bridge service re-asserts ``composing`` chat presence until the
turn settles (``paused``). Pinned here, at the two seams:

- service level: frame shape, keepalive cadence, dedupe, hard cap, kill
  switch, and the no-raise contract (a typing glitch must never break a
  turn);
- runner level: the ``on_turn_active``/``on_turn_settled`` bracket fires
  around the LLM phase on every exit, and never fires on the
  nothing-claimed early return.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.services.dispatch_runner import DispatchSpec
from server.services.whatsapp_bridge_service._service import WhatsAppBridgeService

CHAT = "614000000001@s.whatsapp.net"


class FakeWS:
    def __init__(self):
        self.frames: list[dict] = []

    async def send(self, raw: str) -> None:
        self.frames.append(json.loads(raw))


def _svc(ws=None, *, enabled: bool = True, keepalive: float = 0.01,
         cap: float = 0.2) -> WhatsAppBridgeService:
    svc = object.__new__(WhatsAppBridgeService)
    svc._ws = ws
    svc._get_settings = lambda: SimpleNamespace(  # type: ignore[method-assign]
        whatsapp_bridge=SimpleNamespace(
            typing_indicator_enabled=enabled,
            typing_keepalive_seconds=keepalive,
            typing_max_seconds=cap,
        ))
    return svc


def _presence(ws: FakeWS) -> list[dict]:
    return [f for f in ws.frames if f.get("type") == "send_chat_presence"]


def _states(ws: FakeWS, state: str) -> list[dict]:
    return [f["payload"] for f in _presence(ws)
            if f["payload"]["state"] == state]


# --- Service level ---

async def test_start_typing_sends_single_composing_frame():
    ws = FakeWS()
    svc = _svc(ws)
    await svc.start_typing(CHAT)
    frames = _presence(ws)
    assert len(frames) == 1
    assert frames[0]["payload"] == {
        "chat_id": CHAT, "state": "composing", "media": ""}
    await svc.stop_typing(CHAT)


async def test_double_start_replaces_keepalive():
    ws = FakeWS()
    svc = _svc(ws)
    await svc.start_typing(CHAT)
    first = svc._typing_tasks[CHAT]
    await svc.start_typing(CHAT)
    assert len(svc._typing_tasks) == 1
    assert svc._typing_tasks[CHAT] is not first
    await asyncio.sleep(0.05)
    assert first.done(), "superseded keepalive must be cancelled"
    await svc.stop_typing(CHAT)


async def test_keepalive_reasserts_composing():
    ws = FakeWS()
    svc = _svc(ws)
    await svc.start_typing(CHAT)
    await asyncio.sleep(0.05)
    assert len(_states(ws, "composing")) >= 2, "clients time the indicator out; it must be re-asserted"
    await svc.stop_typing(CHAT)


async def test_stop_typing_clears_indicator():
    ws = FakeWS()
    svc = _svc(ws)
    await svc.start_typing(CHAT)
    await asyncio.sleep(0.03)
    await svc.stop_typing(CHAT)
    assert CHAT not in svc._typing_tasks
    assert len(_states(ws, "paused")) == 1
    composing = len(_states(ws, "composing"))
    await asyncio.sleep(0.05)
    assert len(_states(ws, "composing")) == composing, "no frames after stop"
    assert len(_presence(ws)) == composing + 1


async def test_stop_without_start_is_noop():
    ws = FakeWS()
    svc = _svc(ws)
    await svc.stop_typing(CHAT)
    assert _presence(ws) == []


async def test_kill_switch_blocks_typing():
    ws = FakeWS()
    svc = _svc(ws, enabled=False)
    await svc.start_typing(CHAT)
    await asyncio.sleep(0.02)
    assert _presence(ws) == []
    assert not svc.__dict__.get("_typing_tasks")


async def test_keepalive_stops_at_cap():
    ws = FakeWS()
    svc = _svc(ws, keepalive=0.01, cap=0.05)
    await svc.start_typing(CHAT)
    await asyncio.sleep(0.1)
    assert svc._typing_tasks[CHAT].done(), "cap is the leak backstop"
    n = len(_states(ws, "composing"))
    await asyncio.sleep(0.06)
    assert len(_states(ws, "composing")) == n


async def test_send_chat_presence_never_raises():
    class ExplodingWS:
        async def send(self, raw: str) -> None:
            raise RuntimeError("socket gone")

    svc = _svc(None)
    await svc._send_chat_presence(CHAT, "composing")  # no socket: silent
    svc2 = _svc(ExplodingWS())
    await svc2._send_chat_presence(CHAT, "paused")  # send error: swallowed
    await svc2.start_typing(CHAT)
    await svc2.stop_typing(CHAT)


# --- Spec wiring ---

def _settings(tmp_path: Path):
    """Real Settings with test paths — the spec builder walks many fields."""
    from server.config import Settings
    settings = Settings.from_env()
    settings.whatsapp_bridge.media_dir = tmp_path / "media"
    settings.harness.workspace_dir = tmp_path / "workspace"
    settings.dream.enabled = False
    settings.patience.patience_off_settle_seconds = 0.0
    settings.openai.api_key = settings.openai.api_key or "test-key"
    return settings


async def test_dispatch_spec_carries_typing_hooks(ctx, tmp_path):
    svc = object.__new__(WhatsAppBridgeService)
    svc.ctx = ctx
    svc.db = ctx.db
    svc._ws = None
    svc._presence_subscribed = set()
    ctx.settings = _settings(tmp_path)
    svc._get_settings = lambda: ctx.settings  # type: ignore[method-assign]

    spec = await svc._build_inbound_dispatch_spec(
        session_key="agent:main:whatsapp:dm:614000000001",
        chat_id=CHAT,
        chat_kind="dm",
        contact_id=None,
        is_trusted=False,
        human_initiated=True,
    )
    assert spec.on_turn_active is not None
    assert spec.on_turn_settled is not None

    active, settled = AsyncMock(), AsyncMock()
    svc.start_typing = active  # type: ignore[method-assign]
    svc.stop_typing = settled  # type: ignore[method-assign]
    await spec.on_turn_active()
    await spec.on_turn_settled()
    active.assert_awaited_once_with(CHAT)
    settled.assert_awaited_once_with(CHAT)


# --- Runner level ---

def _spec(session_key: str, **hooks) -> DispatchSpec:
    return DispatchSpec(
        session_key=session_key,
        system_content="system",
        tools=[],
        call_category="whatsapp_incoming",
        send_tool_name="send_whatsapp_message",
        dispatch_id="test-dispatch",
        message_was_sent=[False],
        sent_texts=[],
        **hooks,
    )


async def test_hooks_fire_on_llm_exception(ctx, monkeypatch):
    """Settled must fire even when the turn dies mid-LLM — otherwise the
    indicator dangles (until the cap) on every provider failure."""
    from server.services.dispatch_runner import DispatchRunner
    from server.services.llm_dispatch import LLMDispatchService
    from server.services.session_service import SessionService

    async def _boom(self, messages, tools, **kwargs):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(LLMDispatchService, "chat_with_tools", _boom)

    async def _build(dummy, session_key, **kwargs):
        return [{"role": "user", "content": "hi"}]

    monkeypatch.setattr(
        "server.services.prompt_assembler.build_chat_messages", _build)

    key = "test:typing:exception"
    await SessionService(ctx).add_message(key, "user", "hello", dispatched=0)
    active, settled = AsyncMock(), AsyncMock()
    with pytest.raises(RuntimeError):
        await DispatchRunner(ctx).run(
            _spec(key, on_turn_active=active, on_turn_settled=settled))
    active.assert_awaited_once()
    settled.assert_awaited_once()


async def test_no_hooks_when_nothing_claimed(ctx):
    """The nothing-claimed early return must not start typing."""
    from server.services.dispatch_runner import DispatchRunner

    active, settled = AsyncMock(), AsyncMock()
    result = await DispatchRunner(ctx).run(
        _spec("test:typing:empty",
              on_turn_active=active, on_turn_settled=settled))
    assert result == ""
    active.assert_not_awaited()
    settled.assert_not_awaited()
