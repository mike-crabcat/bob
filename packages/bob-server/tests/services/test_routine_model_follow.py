"""Routine dispatches follow the session's /model override.

fire_routine has its own dispatch path (not DispatchRunner), so this tests
the seam directly: with model_override set on the session, the LLM call
receives the resolved slug; without it, model stays None (global default).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    from bob_server.services import model_registry
    model_registry._models_cache = None
    yield
    model_registry._models_cache = None


def _ctx(db, tmp_path):
    return SimpleNamespace(
        db=db,
        settings=SimpleNamespace(
            harness=SimpleNamespace(workspace_dir=tmp_path),
            config_dir=tmp_path,
            openrouter=SimpleNamespace(enabled=True),
        ),
        whatsapp_bridge=None,
    )


ROUTINE = {
    "id": "r1", "session_key": "wa:123", "name": "greet",
    "schedule": "0 9 * * *", "prompt": "say hi", "enabled": 1,
    "timezone": "Australia/Perth",
}


async def _fire(db, tmp_path, monkeypatch, captured):
    from bob_server.repositories.conversations import ConversationRepository
    from bob_server.services import routine_service

    class _FakeSessionSvc:
        def __init__(self, ctx):
            pass

        async def add_message(self, *a, **k):
            return None

    class _FakeDispatch:
        def __init__(self, ctx):
            pass

        async def chat_with_tools(self, messages, tools, **kwargs):
            captured.update(kwargs)
            return "routine reply"

    async def _fake_route_for(self, session_key):
        return None

    async def _fake_mark_run(self, routine_id):
        return None

    async def _fake_build_messages(prompt, user_message, *, system_content=None, **k):
        return [{"role": "user", "content": prompt}]

    async def _fake_workspace_prompt(*a, **k):
        return ""

    monkeypatch.setattr(
        "bob_server.services.session_service.SessionService", _FakeSessionSvc)
    monkeypatch.setattr(
        "bob_server.services.llm_dispatch.LLMDispatchService", _FakeDispatch)
    monkeypatch.setattr(ConversationRepository, "route_for", _fake_route_for)
    monkeypatch.setattr(routine_service.RoutineService, "mark_run", _fake_mark_run)
    monkeypatch.setattr(
        "bob_server.services.prompt_assembler.build_chat_messages", _fake_build_messages)
    monkeypatch.setattr(
        "bob_server.services.prompt_assembler.load_workspace_prompt", _fake_workspace_prompt)
    monkeypatch.setattr(
        "bob_server.services.tool_registry.build_common_tools",
        lambda *a, **k: [])
    monkeypatch.setattr(
        "bob_server.services.wake_service.session_key_to_chat_id",
        lambda sk: None)

    await routine_service.fire_routine(_ctx(db, tmp_path), dict(ROUTINE))


@pytest.mark.asyncio
async def test_routine_uses_override_model(db, tmp_path, monkeypatch):
    from bob_server.services import model_registry
    from bob_server.repositories.conversations import ConversationRepository

    (tmp_path / "models.yaml").write_text(
        "aliases:\n  chinese: z-ai/glm-5.3-flash\n", encoding="utf-8")
    model_registry._models_cache = None

    repo = ConversationRepository(db)
    await repo.ensure("wa:123")
    await repo.set_policy("wa:123", {"model_override": "chinese"})

    captured: dict = {}
    await _fire(db, tmp_path, monkeypatch, captured)
    assert captured.get("model") == "z-ai/glm-5.3-flash"
    assert captured.get("call_category") == "routine"


@pytest.mark.asyncio
async def test_routine_without_override_uses_default(db, tmp_path, monkeypatch):
    from bob_server.repositories.conversations import ConversationRepository
    await ConversationRepository(db).ensure("wa:123")

    captured: dict = {}
    await _fire(db, tmp_path, monkeypatch, captured)
    assert captured.get("model") is None
