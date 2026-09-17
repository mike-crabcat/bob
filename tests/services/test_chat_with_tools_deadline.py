"""chat_with_tools wall-clock budget: checked before each iteration, so a
turn with a spent budget makes no further tool rounds. With self-wrap
enabled, exhaustion makes exactly one forced wrap-up round (tools stripped);
disabled, it returns the canned stop message with no LLM calls at all.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.services.openai_service import OpenAIService


def _service(monkeypatch, tmp_path, *, wrap_enabled=True, wrap_text="wrapped up"):
    """OpenAIService with no network: the fake client records calls and (for
    the forced wrap-up round) returns ``wrap_text``."""
    svc = object.__new__(OpenAIService)
    settings = SimpleNamespace(
        openai=SimpleNamespace(
            api_key="test-key", base_url="http://localhost:1",
            default_model="gpt-test", memory_model="",
            web_search_enabled=False),
        openrouter=SimpleNamespace(enabled=False),
        self_wrap=SimpleNamespace(
            enabled=wrap_enabled, duration_fraction=0.75, iteration_margin=3),
    )
    svc._get_settings = lambda: settings

    calls: list[dict] = []

    class _Fake:
        async def create(self, **kwargs):
            calls.append({**kwargs, "input": list(kwargs.get("input", []))})
            return SimpleNamespace(
                output=[SimpleNamespace(
                    type="message", role="assistant",
                    content=[SimpleNamespace(type="output_text", text=wrap_text)])],
                output_text=wrap_text, usage=None, status="completed",
                refusal=None,
            )

    def _client_for(model):
        return SimpleNamespace(responses=_Fake())

    svc._client_for = lambda model: _client_for(model)
    return svc, calls


@pytest.mark.asyncio
async def test_spent_budget_single_forced_wrapup_round(monkeypatch, tmp_path):
    """Self-wrap on: a spent budget makes no tool rounds — just one forced
    wrap-up call with tools stripped, returning the model's own closing text."""
    svc, calls = _service(monkeypatch, tmp_path)
    result = await svc.chat_with_tools(
        [{"role": "user", "content": "bulletin"}],
        tools=[], tool_handlers={},
        time_limit_seconds=0.0,
    )
    assert result == "wrapped up"
    assert len(calls) == 1
    assert "tools" not in calls[0]


@pytest.mark.asyncio
async def test_spent_budget_disabled_canned_stop(monkeypatch, tmp_path):
    """Self-wrap off: legacy contract — zero LLM calls, canned stop message."""
    svc, calls = _service(monkeypatch, tmp_path, wrap_enabled=False)
    result = await svc.chat_with_tools(
        [{"role": "user", "content": "bulletin"}],
        tools=[], tool_handlers={},
        time_limit_seconds=0.0,
    )
    assert "wall-clock budget" in result
    assert calls == []


@pytest.mark.asyncio
async def test_no_budget_keeps_existing_contract(monkeypatch, tmp_path):
    """time_limit_seconds=None must behave exactly as before (no deadline
    arithmetic, no extra stop path)."""
    svc, calls = _service(monkeypatch, tmp_path)
    # The wrap_text fake would swallow the call, so assert the None path
    # reaches the client as a normal (tooled) call — reusing the recording
    # fake, a normal first call carries the tools kwarg.
    await svc.chat_with_tools(
        [{"role": "user", "content": "hi"}],
        tools=[], tool_handlers={},
        time_limit_seconds=None,
    )
    assert len(calls) == 1
    assert "tools" in calls[0]


def _send_tool_dict(name: str = "send_whatsapp_message") -> dict:
    return {"type": "function", "function": {
        "name": name, "description": "send", "parameters": {}}}


def _system_contents(call: dict) -> str:
    return "\n".join(
        m.get("content", "") for m in call["input"]
        if m.get("role") == "system")


@pytest.mark.asyncio
async def test_send_tool_turn_gets_delivery_wrap_text(monkeypatch, tmp_path):
    """2026-09-14 crypto-report incident: on send-tool channels 'reply to
    the user' delivers nothing — the tools-stripped wrap-up round must use
    the delivery-framed text, and budget_stats must flag the cutoff."""
    from server.services.openai_service import _SELF_WRAP_FINAL_SEND
    svc, calls = _service(monkeypatch, tmp_path)
    budget: dict[str, bool] = {}
    result = await svc.chat_with_tools(
        [{"role": "user", "content": "report"}],
        tools=[_send_tool_dict()], tool_handlers={},
        time_limit_seconds=0.0, budget_stats=budget,
    )
    assert result == "wrapped up"
    assert _SELF_WRAP_FINAL_SEND in _system_contents(calls[0])
    assert budget.get("hit_wall_clock") is True


@pytest.mark.asyncio
async def test_plain_turn_keeps_original_wrap_text(monkeypatch, tmp_path):
    from server.services.openai_service import _SELF_WRAP_FINAL
    svc, calls = _service(monkeypatch, tmp_path)
    budget: dict[str, bool] = {}
    await svc.chat_with_tools(
        [{"role": "user", "content": "hi"}],
        tools=[_send_tool_dict("bash")], tool_handlers={},
        time_limit_seconds=0.0, budget_stats=budget,
    )
    assert _SELF_WRAP_FINAL in _system_contents(calls[0])
    assert budget.get("hit_wall_clock") is True


def test_strip_wrap_nudges_covers_send_variants():
    from server.services.openai_service import (
        _SELF_WRAP_NUDGE, _SELF_WRAP_FINAL,
        _SELF_WRAP_NUDGE_SEND, _SELF_WRAP_FINAL_SEND,
        _strip_wrap_nudges,
    )
    messages = [
        {"role": "system", "content": _SELF_WRAP_NUDGE},
        {"role": "user", "content": "keep me"},
        {"role": "system", "content": _SELF_WRAP_FINAL},
        {"role": "system", "content": _SELF_WRAP_NUDGE_SEND},
        {"role": "system", "content": _SELF_WRAP_FINAL_SEND},
    ]
    _strip_wrap_nudges(messages)
    assert messages == [{"role": "user", "content": "keep me"}]
