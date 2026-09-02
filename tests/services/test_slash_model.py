"""Tests for the /model slash command (SlashCommandsMixin._cmd_model)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bob_server.services.whatsapp_bridge_service._service import WhatsAppBridgeService

YAML = """\
aliases:
  cheap: gpt-5.6-luna
  chinese: z-ai/glm-5.3-flash
pricing:
  z-ai/glm-5.3-flash: [0.075, 0.25]
"""


def _make_service(db, tmp_path, *, openrouter_enabled):
    from bob_server.services import model_registry
    model_registry._models_cache = None
    (tmp_path / "models.yaml").write_text(YAML, encoding="utf-8")
    svc = object.__new__(WhatsAppBridgeService)
    svc.db = db
    svc.ctx = SimpleNamespace(
        db=db,
        settings=SimpleNamespace(
            config_dir=tmp_path,
            openai=SimpleNamespace(default_model="gpt-5.6-sol"),
            openrouter=SimpleNamespace(enabled=openrouter_enabled),
        ),
    )
    svc.send_message = AsyncMock()
    return svc


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    from bob_server.services import model_registry
    model_registry._models_cache = None
    yield
    model_registry._models_cache = None


@pytest.mark.asyncio
async def test_status_shows_default(db, tmp_path):
    svc = _make_service(db, tmp_path, openrouter_enabled=True)
    await svc._cmd_model("", "wa:123", "chat-1")
    text = svc.send_message.call_args.args[1]
    assert "gpt-5.6-sol (default)" in text
    assert "chinese" in text


@pytest.mark.asyncio
async def test_set_alias_and_status(db, tmp_path):
    from bob_server.repositories.conversations import ConversationRepository
    svc = _make_service(db, tmp_path, openrouter_enabled=True)
    await svc._cmd_model("chinese", "wa:123", "chat-1")
    text = svc.send_message.call_args.args[1]
    assert "z-ai/glm-5.3-flash" in text

    policy = await ConversationRepository(db).get_policy("wa:123")
    assert policy.get("model_override") == "chinese"

    svc.send_message.reset_mock()
    await svc._cmd_model("", "wa:123", "chat-1")
    text = svc.send_message.call_args.args[1]
    assert "chinese → z-ai/glm-5.3-flash" in text


@pytest.mark.asyncio
async def test_set_plain_model(db, tmp_path):
    from bob_server.repositories.conversations import ConversationRepository
    svc = _make_service(db, tmp_path, openrouter_enabled=False)
    await svc._cmd_model("gpt-5.6-terra", "wa:123", "chat-1")
    policy = await ConversationRepository(db).get_policy("wa:123")
    assert policy.get("model_override") == "gpt-5.6-terra"


@pytest.mark.asyncio
async def test_rejects_openrouter_when_unconfigured(db, tmp_path):
    from bob_server.repositories.conversations import ConversationRepository
    svc = _make_service(db, tmp_path, openrouter_enabled=False)
    await svc._cmd_model("chinese", "wa:123", "chat-1")
    text = svc.send_message.call_args.args[1]
    assert "OpenRouter" in text
    policy = await ConversationRepository(db).get_policy("wa:123")
    assert not policy.get("model_override")


@pytest.mark.asyncio
async def test_plain_unknown_name_is_allowed(db, tmp_path):
    """Plain names can't be verified against OpenAI's catalog offline — they
    pass through (plan decision); only malformed names are rejected."""
    from bob_server.repositories.conversations import ConversationRepository
    svc = _make_service(db, tmp_path, openrouter_enabled=True)
    await svc._cmd_model("turbo", "wa:123", "chat-1")
    policy = await ConversationRepository(db).get_policy("wa:123")
    assert policy.get("model_override") == "turbo"


@pytest.mark.asyncio
async def test_malformed_name_lists_aliases(db, tmp_path):
    svc = _make_service(db, tmp_path, openrouter_enabled=True)
    await svc._cmd_model("not a model!", "wa:123", "chat-1")
    text = svc.send_message.call_args.args[1]
    assert "not a model!" in text
    assert "cheap" in text  # alias list included


@pytest.mark.asyncio
async def test_unknown_command_gets_reply(db, tmp_path):
    """Regression: unknown slash commands used to fall through silently."""
    svc = _make_service(db, tmp_path, openrouter_enabled=True)
    await svc._handle_slash_command(
        "/chinese", "wa:123", "chat-1", "dm", "jid", "Mike")
    text = svc.send_message.call_args.args[1]
    assert "isn't a command" in text
    assert "/model" in text


@pytest.mark.asyncio
async def test_reset_clears_override(db, tmp_path):
    from bob_server.repositories.conversations import ConversationRepository
    svc = _make_service(db, tmp_path, openrouter_enabled=True)
    await svc._cmd_model("cheap", "wa:123", "chat-1")
    await svc._cmd_model("default", "wa:123", "chat-1")
    text = svc.send_message.call_args.args[1]
    assert "default" in text
    policy = await ConversationRepository(db).get_policy("wa:123")
    assert not policy.get("model_override")
