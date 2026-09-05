"""Routine send tool accepts media_path, same as conversation turns.

2026-09-05: the 07:00 crypto-morning-report couldn't attach its charts —
fire_routine's send_whatsapp_message wrapper only accepted ``text``
(``got an unexpected keyword argument 'media_path'``). All three wrappers
(main turns, routines, group events) now share resolve_sendable_media; this
pins the routine seam and the guard matrix. The group-events wrapper mirrors
the main wrapper's media branch (effects outbox, kind=whatsapp_send_media).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    from server.services import model_registry
    model_registry._models_cache = None
    yield
    model_registry._models_cache = None


class _FakeBridge:
    connected = True

    def __init__(self):
        self.calls: list[tuple] = []

    async def send_message(self, chat_id, text, *, reply_to=None):
        self.calls.append(("text", chat_id, text))
        return "req-t1"

    async def send_media(self, chat_id, file_path, *, caption=""):
        self.calls.append(("media", chat_id, file_path, caption))
        return "req-m1"


def _ctx(db, tmp_path, bridge):
    return SimpleNamespace(
        db=db,
        settings=SimpleNamespace(
            harness=SimpleNamespace(workspace_dir=tmp_path),
            config_dir=tmp_path,
            openai=SimpleNamespace(default_model="gpt-test-default"),
            openrouter=SimpleNamespace(enabled=True),
        ),
        whatsapp_bridge=bridge,
    )


ROUTINE = {
    "id": "r1", "session_key": "wa:123", "name": "charts",
    "schedule": "0 7 * * *", "prompt": "post charts", "enabled": 1,
    "timezone": "Australia/Perth",
}


async def _fire(db, tmp_path, monkeypatch, captured):
    from server.repositories.conversations import ConversationRepository
    from server.services import routine_service

    class _FakeSessionSvc:
        def __init__(self, ctx):
            pass

        async def add_message(self, *a, **k):
            return None

    class _FakeDispatch:
        def __init__(self, ctx):
            pass

        async def chat_with_tools(self, messages, tools, **kwargs):
            captured["tools"] = tools
            return "routine reply"

    async def _fake_route_for(self, session_key):
        return None

    async def _fake_mark_run(self, routine_id):
        return None

    async def _fake_build_messages(prompt, user_message, **k):
        return [{"role": "user", "content": prompt}]

    async def _fake_workspace_prompt(*a, **k):
        return ""

    monkeypatch.setattr(
        "server.services.session_service.SessionService", _FakeSessionSvc)
    monkeypatch.setattr(
        "server.services.llm_dispatch.LLMDispatchService", _FakeDispatch)
    monkeypatch.setattr(ConversationRepository, "route_for", _fake_route_for)
    monkeypatch.setattr(routine_service.RoutineService, "mark_run", _fake_mark_run)
    monkeypatch.setattr(
        "server.services.prompt_assembler.build_chat_messages", _fake_build_messages)
    monkeypatch.setattr(
        "server.services.prompt_assembler.load_workspace_prompt", _fake_workspace_prompt)
    monkeypatch.setattr(
        "server.services.tool_registry.build_common_tools",
        lambda *a, **k: [])
    monkeypatch.setattr(
        "server.services.wake_service.session_key_to_chat_id",
        lambda sk: "1203@g.us")

    bridge = _FakeBridge()
    await routine_service.fire_routine(_ctx(db, tmp_path, bridge), dict(ROUTINE))
    return bridge


def _send_tool(captured):
    return next(t for t in captured["tools"] if t.name == "send_whatsapp_message")


@pytest.mark.asyncio
async def test_routine_send_tool_sends_media(db, tmp_path, monkeypatch):
    media = tmp_path / "generated-images" / "chart-SOL-24h.mp4"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"\x00\x00\x00\x18ftypmp42")

    captured: dict = {}
    bridge = await _fire(db, tmp_path, monkeypatch, captured)
    tool = _send_tool(captured)

    assert "media_path" in tool.parameters  # schema declares it

    out = await tool.handler(
        text="SOL 24h chart", media_path="generated-images/chart-SOL-24h.mp4")
    assert out.startswith("Media sent")
    assert bridge.calls == [
        ("media", "1203@g.us", str(media.resolve()), "SOL 24h chart")]


@pytest.mark.asyncio
async def test_routine_send_tool_text_only(db, tmp_path, monkeypatch):
    captured: dict = {}
    bridge = await _fire(db, tmp_path, monkeypatch, captured)
    tool = _send_tool(captured)

    out = await tool.handler(text="hello")
    assert out.startswith("Message sent")
    assert bridge.calls == [("text", "1203@g.us", "hello")]


@pytest.mark.asyncio
async def test_routine_send_tool_rejects_escape_and_missing(db, tmp_path, monkeypatch):
    captured: dict = {}
    bridge = await _fire(db, tmp_path, monkeypatch, captured)
    tool = _send_tool(captured)

    assert await tool.handler(text="x", media_path="../../etc/passwd") == \
        "Error: path escapes workspace"
    assert await tool.handler(text="x", media_path="generated-images/absent.mp4") == \
        "Error: file not found: generated-images/absent.mp4"
    assert bridge.calls == []


def test_resolve_sendable_media_matrix(tmp_path):
    from server.services.whatsapp_bridge_service._media import resolve_sendable_media

    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x")
    assert resolve_sendable_media(tmp_path, "clip.mp4") == f
    assert resolve_sendable_media(tmp_path, "nope.mp4") == \
        "Error: file not found: nope.mp4"
    assert resolve_sendable_media(tmp_path, "../escape.mp4") == \
        "Error: path escapes workspace"
