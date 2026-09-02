"""cost_rollup_24h groups by provider — the basis of the dashboard split."""

from __future__ import annotations

import pytest

from server.repositories.llm_call_log import LlmCallLogRepository


async def _log(db, **kwargs):
    await LlmCallLogRepository(db).upsert(
        provider=kwargs.get("provider", "openai"),
        model=kwargs.get("model", "gpt-5.6-sol"),
        call_category=kwargs.get("call_category", "whatsapp_chat"),
        status=kwargs.get("status", "completed"),
        prompt_tokens=kwargs.get("prompt_tokens", 1000),
        completion_tokens=kwargs.get("completion_tokens", 1000),
    )


@pytest.mark.asyncio
async def test_rollup_splits_providers(db):
    await _log(db, provider="openai", model="gpt-5.6-sol", call_category="whatsapp_chat")
    await _log(db, provider="openai", model="gpt-5.6-sol", call_category="whatsapp_chat")
    await _log(db, provider="openrouter", model="z-ai/glm-5.3-flash", call_category="whatsapp_chat")

    rows = await LlmCallLogRepository(db).cost_rollup_24h()
    by_key = {(r["call_category"], r["model"], r["provider"]): r for r in rows}
    assert by_key[("whatsapp_chat", "gpt-5.6-sol", "openai")]["call_count"] == 2
    assert by_key[("whatsapp_chat", "z-ai/glm-5.3-flash", "openrouter")]["call_count"] == 1


@pytest.mark.asyncio
async def test_legacy_rows_without_provider_group_under_openai(db):
    await _log(db, provider="", model="gpt-5.6-sol")
    rows = await LlmCallLogRepository(db).cost_rollup_24h()
    assert rows and all(r["provider"] in ("", "openai") for r in rows)
