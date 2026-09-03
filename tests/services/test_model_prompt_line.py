"""Turn-scoped model line (2026-09-03).

The persona used to claim a static model, but model switching went live
(global default from env/models.yaml + per-chat /model override), so the
identity no longer names one. Pinned here:
- the line names the serving slug and its source (global default vs
  per-chat /model override)
- build_chat_messages appends it AFTER the clock, idempotently, only when
  the caller resolved a model; include_time=False still carries it
- DispatchRunner resolves the session model before building messages, so
  the line states the real serving slug on real dispatch turns
"""

from __future__ import annotations

import pytest

from server.services.prompt_assembler import (
    build_chat_messages,
    model_serving_prompt_line,
)


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    from server.services import model_registry
    model_registry._models_cache = None
    yield
    model_registry._models_cache = None


def test_line_names_slug_and_source():
    assert model_serving_prompt_line("z-ai/glm-5.3-flash") == (
        "Model serving this turn: z-ai/glm-5.3-flash (global default).")
    assert model_serving_prompt_line("gpt-5.6-luna", override=True) == (
        "Model serving this turn: gpt-5.6-luna (per-chat /model override).")


async def test_build_chat_messages_appends_model_after_clock():
    messages = await build_chat_messages(
        "hi", "", db=None, system_content="SYS",
        current_model="z-ai/glm-5.3-flash")
    content = messages[0]["content"]
    assert content.startswith("SYS\n\nLocal time now: ")
    assert ("Model serving this turn: z-ai/glm-5.3-flash (global default)."
            in content)
    # appended after the clock, never inside the stable prefix
    assert (content.index("Local time now:")
            < content.index("Model serving this turn:"))
    assert content.count("Model serving this turn:") == 1


async def test_no_current_model_leaves_shape_unchanged():
    messages = await build_chat_messages("hi", "", db=None, system_content="SYS")
    assert "Model serving this turn:" not in messages[0]["content"]


async def test_model_line_injection_is_idempotent():
    messages = await build_chat_messages(
        "hi", "", db=None,
        system_content=model_serving_prompt_line("z-ai/glm-5.3-flash"),
        current_model="z-ai/glm-5.3-flash")
    assert messages[0]["content"].count("Model serving this turn:") == 1


async def test_model_line_survives_include_time_false():
    messages = await build_chat_messages(
        "hi", "", db=None, system_content="SYS", include_time=False,
        current_model="z-ai/glm-5.3-flash", current_model_override=True)
    content = messages[0]["content"]
    assert "Local time now:" not in content
    assert ("Model serving this turn: z-ai/glm-5.3-flash "
            "(per-chat /model override)." in content)


@pytest.mark.asyncio
async def test_dispatch_runner_threads_resolved_model(ctx, db, monkeypatch, tmp_path):
    """Real dispatch turns: the runner resolves the session model BEFORE
    building messages, so the turn-scoped line states the real serving slug —
    override source when /model set one, global default otherwise."""
    from server.repositories.conversations import ConversationRepository
    from server.services import model_registry
    from server.services.dispatch_runner import DispatchRunner, DispatchSpec
    from server.services.llm_dispatch import LLMDispatchService
    from server.services.session_service import SessionService

    (tmp_path / "models.yaml").write_text(
        "aliases:\n  chinese: z-ai/glm-5.3-flash\n", encoding="utf-8")
    monkeypatch.setattr(ctx.settings, "config_dir", tmp_path)
    model_registry._models_cache = None

    key = "test:model:runner"
    repo = ConversationRepository(db)
    await repo.ensure(key)
    await repo.set_policy(key, {"model_override": "chinese"})
    await SessionService(ctx).add_message(key, "user", "hello", dispatched=0)

    seen: dict = {}

    async def _chat(self, messages, tools, **kwargs):
        seen["messages"] = messages
        return ""

    monkeypatch.setattr(LLMDispatchService, "chat_with_tools", _chat)

    async def _run():
        spec = DispatchSpec(
            session_key=key, system_content="sys", tools=[],
            call_category="whatsapp_incoming",
            send_tool_name="send_whatsapp_message",
            dispatch_id="test-dispatch-model",
            message_was_sent=[False], sent_texts=[],
        )
        await DispatchRunner(ctx).run(spec)

    await _run()
    assert seen["messages"], "LLM must have been called"
    system = seen["messages"][0]["content"]
    assert ("Model serving this turn: z-ai/glm-5.3-flash "
            "(per-chat /model override)." in system)

    # Revert the override: next turn reports the global default. The first
    # run consumed the pending message, so seed a fresh one.
    await repo.set_policy(key, {"model_override": ""})
    await SessionService(ctx).add_message(key, "user", "again", dispatched=0)
    seen.clear()
    await _run()
    system = seen["messages"][0]["content"]
    assert (f"Model serving this turn: {ctx.settings.openai.default_model} "
            "(global default)." in system)
