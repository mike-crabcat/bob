"""Tests for the self-wrap budget nudges in OpenAIService.chat_with_tools.

Budget exhaustion is a two-stage wrap-up (settings.self_wrap): a soft one-shot
system nudge near the time/iteration budget, then a forced final LLM round
with tools stripped at the deadline or iteration cap. With self_wrap disabled
the legacy canned-string stop applies. Nudge messages must never survive in
the caller's messages list.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bob_server.services.openai_service import (
    OpenAIService,
    _SELF_WRAP_FINAL,
    _SELF_WRAP_NUDGE,
)


class FakeClock:
    """Deterministic monotonic clock: tests advance it inside fake LLM calls."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _tool_call_response(name: str = "probe_tool", call_id: str = "call-1"):
    return SimpleNamespace(
        output=[SimpleNamespace(
            type="function_call", call_id=call_id, name=name, arguments="{}",
        )],
        output_text="", usage=None, status="completed", refusal=None,
    )


def _text_response(text: str):
    return SimpleNamespace(
        output=[SimpleNamespace(
            type="message", role="assistant",
            content=[SimpleNamespace(type="output_text", text=text)],
        )],
        output_text=text,
        usage=SimpleNamespace(total_tokens=10, input_tokens=5, output_tokens=5),
        status="completed", refusal=None,
    )


@pytest.fixture
def install_client(monkeypatch):
    """Install a fake OpenAI client on OpenAIService.client.

    Returns a factory: call it with either a response script (list returned in
    order) or seconds_per_call for an endless tool-call loop; when the forced
    wrap-up round arrives (no `tools` kwarg), the client returns a final text.
    Returns (calls, clock) — calls records each create() kwargs.
    """
    def install(*, script: list | None = None, seconds_per_call: float = 0.0):
        calls: list[dict] = []
        clock = FakeClock()
        i = 0

        async def fake_create(**kwargs):
            nonlocal i
            # snapshot input — it's the caller's live list, mutated (and
            # nudge-stripped) after the call returns
            calls.append({**kwargs, "input": list(kwargs.get("input", []))})
            if "tools" not in kwargs:
                # forced wrap-up round
                return _text_response("here is what I have so far")
            if script is not None:
                resp = script[min(i, len(script) - 1)]
                i += 1
                return resp
            clock.advance(seconds_per_call)
            return _tool_call_response(call_id=f"call-{i}")

        fake_client = SimpleNamespace(
            responses=SimpleNamespace(create=fake_create))
        monkeypatch.setattr(
            OpenAIService, "client", property(lambda self: fake_client))
        # the service module reads time.monotonic(); pin it to the fake clock
        import bob_server.services.openai_service as svc_mod
        monkeypatch.setattr(
            svc_mod, "time", SimpleNamespace(monotonic=clock.monotonic))
        return calls, clock

    return install


async def _run(svc, messages, **kwargs):
    async def handler(**kw):
        return "tool result"

    return await svc.chat_with_tools(
        messages,
        [],
        {"probe_tool": handler},
        model="gpt-5.6-sol",
        dispatch_id="dispatch-selfwrap",
        session_key="sess-selfwrap",
        **kwargs,
    )


def _nudge_count(calls: list[dict]) -> int:
    def has_nudge(kwargs):
        return any(m.get("content") == _SELF_WRAP_NUDGE
                   for m in kwargs.get("input", []) if isinstance(m, dict))
    return sum(1 for c in calls if has_nudge(c))


async def test_duration_nudge_then_forced_wrapup(ctx, install_client):
    """Soft nudge at 75% of the time budget; forced tools-free final round at
    the deadline returns the model's own wrap-up text."""
    calls, _ = install_client(seconds_per_call=30.0)
    svc = OpenAIService(ctx)
    messages = [{"role": "user", "content": "check the batches"}]

    result = await _run(svc, messages, time_limit_seconds=100)

    assert result == "here is what I have so far"
    # the soft nudge was injected exactly once, and only into calls made
    # after the 75% mark (elapsed 90s of a 100s budget by call 4)
    assert _nudge_count(calls) >= 1
    assert all("tools" in c for c in calls[:-1])
    # the final round is the forced wrap-up: no tools offered, final nudge in
    final = calls[-1]
    assert "tools" not in final
    assert any(m.get("content") == _SELF_WRAP_FINAL
               for m in final["input"] if isinstance(m, dict))
    # nudges never survive in the caller's messages list
    assert all(m.get("content") not in (_SELF_WRAP_NUDGE, _SELF_WRAP_FINAL)
               for m in messages if isinstance(m, dict))


async def test_iteration_nudge_then_forced_wrapup(ctx, install_client):
    """No time limit: the nudge fires 3 iterations before the cap, and when
    the cap is reached mid-tool-loop a forced wrap-up round ends the turn."""
    calls, _ = install_client(seconds_per_call=0.0)
    svc = OpenAIService(ctx)
    messages = [{"role": "user", "content": "check the batches"}]

    result = await _run(svc, messages, max_iterations=6)

    assert result == "here is what I have so far"
    tool_rounds = [c for c in calls if "tools" in c]
    assert len(tool_rounds) == 6  # loop ran to the cap still calling tools
    # nudge present from iteration 3 onward (margin 3), exactly one injection
    # (the forced round also carries it — it stays in messages until stripped)
    assert _nudge_count(tool_rounds) == 3  # iterations 3, 4, 5 see it
    assert "tools" not in calls[-1]
    assert all(m.get("content") not in (_SELF_WRAP_NUDGE, _SELF_WRAP_FINAL)
               for m in messages if isinstance(m, dict))


async def test_fast_turn_never_nudged(ctx, install_client):
    """A turn that finishes quickly sees no nudge and no forced round."""
    calls, _ = install_client(script=[_tool_call_response(), _text_response("done")])
    svc = OpenAIService(ctx)

    result = await _run(svc, [{"role": "user", "content": "quick one"}],
                        time_limit_seconds=100)

    assert result == "done"
    assert _nudge_count(calls) == 0
    assert all("tools" in c for c in calls)  # no forced wrap-up round


async def test_disabled_reverts_to_canned_stop(ctx, install_client):
    """self_wrap off: the deadline returns the legacy canned string and no
    forced round is attempted."""
    ctx.settings.self_wrap.enabled = False
    calls, _ = install_client(seconds_per_call=30.0)
    svc = OpenAIService(ctx)

    result = await _run(svc, [{"role": "user", "content": "loop forever"}],
                        time_limit_seconds=100)

    assert result.startswith("Stopped at the turn's wall-clock budget")
    assert _nudge_count(calls) == 0
    assert all("tools" in c for c in calls)
