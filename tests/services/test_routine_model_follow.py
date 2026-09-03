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
    from server.services import model_registry
    model_registry._models_cache = None
    yield
    model_registry._models_cache = None


def _ctx(db, tmp_path):
    return SimpleNamespace(
        db=db,
        settings=SimpleNamespace(
            harness=SimpleNamespace(workspace_dir=tmp_path),
            config_dir=tmp_path,
            openai=SimpleNamespace(default_model="gpt-test-default"),
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
            captured.update(kwargs)
            return "routine reply"

    async def _fake_route_for(self, session_key):
        return None

    async def _fake_mark_run(self, routine_id):
        return None

    async def _fake_build_messages(prompt, user_message, *, system_content=None,
                                   current_model=None,
                                   current_model_override=None, **k):
        captured["current_model"] = current_model
        captured["current_model_override"] = current_model_override
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
        lambda sk: None)

    await routine_service.fire_routine(_ctx(db, tmp_path), dict(ROUTINE))


@pytest.mark.asyncio
async def test_routine_uses_override_model(db, tmp_path, monkeypatch):
    from server.services import model_registry
    from server.repositories.conversations import ConversationRepository

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
    # The turn-scoped model line rides the system message with the override.
    assert captured.get("current_model") == "z-ai/glm-5.3-flash"
    assert captured.get("current_model_override") is True


@pytest.mark.asyncio
async def test_routine_carries_wall_clock_budget(db, tmp_path, monkeypatch):
    """Routine dispatches must pass the wall-clock budget — an hourly
    bulletin can't be allowed to become a 10-minute tool odyssey
    (2026-08-29: recovery turn + two routines ran 10.6 min on the radio
    group)."""
    from server.services.routine_service import ROUTINE_WALL_CLOCK_SECONDS

    captured: dict = {}
    await _fire(db, tmp_path, monkeypatch, captured)
    assert captured.get("time_limit_seconds") == ROUTINE_WALL_CLOCK_SECONDS
    assert ROUTINE_WALL_CLOCK_SECONDS == 120.0


@pytest.mark.asyncio
async def test_routine_without_override_uses_default(db, tmp_path, monkeypatch):
    from server.repositories.conversations import ConversationRepository
    await ConversationRepository(db).ensure("wa:123")

    captured: dict = {}
    await _fire(db, tmp_path, monkeypatch, captured)
    assert captured.get("model") is None
    # No override: the model line reports the global default.
    assert captured.get("current_model") == "gpt-test-default"
    assert captured.get("current_model_override") is False
