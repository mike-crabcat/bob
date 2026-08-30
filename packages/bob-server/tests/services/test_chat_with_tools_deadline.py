"""chat_with_tools wall-clock budget: checked before each iteration, so a
turn with a spent budget makes no further LLM calls and returns an explicit
stop message instead of running an unbounded tool odyssey.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bob_server.services.openai_service import OpenAIService


def _service(monkeypatch, tmp_path):
    """OpenAIService with no network: the fake client fails the test if
    reached."""
    svc = object.__new__(OpenAIService)
    settings = SimpleNamespace(
        openai=SimpleNamespace(
            api_key="test-key", base_url="http://localhost:1",
            default_model="gpt-test", memory_model="",
            web_search_enabled=False),
        openrouter=SimpleNamespace(enabled=False),
    )
    svc._get_settings = lambda: settings

    class _Boom:
        async def create(self, **kwargs):
            raise AssertionError("LLM client reached despite spent wall-clock budget")

    def _client_for(model):
        return SimpleNamespace(responses=_Boom())

    svc._client_for = lambda model: _client_for(model)
    return svc


@pytest.mark.asyncio
async def test_spent_budget_stops_before_first_call(monkeypatch, tmp_path):
    svc = _service(monkeypatch, tmp_path)
    result = await svc.chat_with_tools(
        [{"role": "user", "content": "bulletin"}],
        tools=[], tool_handlers={},
        time_limit_seconds=0.0,
    )
    assert "wall-clock budget" in result


@pytest.mark.asyncio
async def test_no_budget_keeps_existing_contract(monkeypatch, tmp_path):
    """time_limit_seconds=None must behave exactly as before (no deadline
    arithmetic, no extra stop path)."""
    svc = _service(monkeypatch, tmp_path)
    # The fake client raises AssertionError on use; with no budget the call
    # WOULD reach the client — so instead assert the parameter accepts None
    # and the deadline branch is inert by checking a stub-out of the loop
    # entry: simplest observable contract is that None doesn't raise during
    # setup, which the successful construction + call attempt shows.
    with pytest.raises(AssertionError, match="LLM client reached"):
        await svc.chat_with_tools(
            [{"role": "user", "content": "hi"}],
            tools=[], tool_handlers={},
            time_limit_seconds=None,
        )
