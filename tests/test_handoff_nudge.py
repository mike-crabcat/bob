"""Batch 1 of the backburner retirement (2026-10-15): hold mode is the
default, a main turn still running after handoff_after_seconds is nudged to
hand long work to a goal, and foreground bash is capped inside main turns.

Origin: two concurrent turns in one DM sent Printful the same support
ticket twice (2026-10-10) — detached flights let a second turn run beside
the first.
"""

from __future__ import annotations

import asyncio

from server.config import BackburnerSettings
from server.services.openai_service import _HANDOFF_NUDGE, OpenAIService
from tests.test_self_wrap import (  # noqa: F401  (fixture)
    _run,
    _text_response,
    _tool_call_response,
    install_client,
)


def _handoff_count(calls: list[dict]) -> int:
    return sum(1 for c in calls
               if any(m.get("content") == _HANDOFF_NUDGE
                      for m in c.get("input", []) if isinstance(m, dict)))


def test_defaults_are_hold_with_short_budgets():
    s = BackburnerSettings()
    assert s.mode == "hold"
    assert s.max_run_seconds == 300.0
    assert s.handoff_after_seconds == 60.0
    assert s.main_turn_bash_timeout_seconds == 90.0


def test_handoff_nudge_names_goals_not_subagents():
    assert "add_goal" in _HANDOFF_NUDGE
    assert "subagent" not in _HANDOFF_NUDGE.lower()
    assert "claude" not in _HANDOFF_NUDGE.lower()


async def test_handoff_nudge_fires_once_after_threshold_and_is_stripped(ctx, install_client):
    calls, _ = install_client(seconds_per_call=25.0)
    svc = OpenAIService(ctx)
    messages = [{"role": "user", "content": "keep polling printful"}]

    await _run(svc, messages, time_limit_seconds=300, max_iterations=6,
               handoff_after_seconds=60)

    tool_rounds = [c for c in calls if "tools" in c]
    # 25 s per call: elapsed 0/25/50 → no nudge; from 75 s on it's present
    assert _handoff_count(tool_rounds[:3]) == 0
    assert _handoff_count(tool_rounds[3:]) == len(tool_rounds[3:]) >= 1
    injected = [m for m in tool_rounds[-1]["input"]
                if isinstance(m, dict) and m.get("content") == _HANDOFF_NUDGE]
    assert len(injected) == 1  # one-shot
    assert all(m.get("content") != _HANDOFF_NUDGE
               for m in messages if isinstance(m, dict))


async def test_fast_turn_and_non_main_turns_get_no_handoff(ctx, install_client):
    calls, _ = install_client(script=[_tool_call_response(), _text_response("done")])
    svc = OpenAIService(ctx)
    assert await _run(svc, [{"role": "user", "content": "quick"}],
                      handoff_after_seconds=60) == "done"
    assert _handoff_count(calls) == 0

    calls, _ = install_client(seconds_per_call=40.0)
    await _run(OpenAIService(ctx), [{"role": "user", "content": "routine"}],
               max_iterations=4)  # no handoff_after_seconds: routines, wakes, goal rooms
    assert _handoff_count(calls) == 0


async def test_turn_bash_cap_applies_only_when_set(ctx, tmp_path):
    from server.services import workspace_tools as wt

    ctx.settings.harness.workspace_dir = tmp_path
    bash = next(t for t in wt.make_workspace_tools(ctx) if t.name == "bash")

    token = wt.TURN_BASH_TIMEOUT.set(0.5)
    try:
        out = str(await bash.handler(command="sleep 3"))
    finally:
        wt.TURN_BASH_TIMEOUT.reset(token)
    assert "timed out after 0s" in out or "timed out after 1s" in out
    assert "add_goal" in out and "run_bg_process" in out

    assert wt.TURN_BASH_TIMEOUT.get() is None
    out = str(await asyncio.wait_for(bash.handler(command="echo ok"), timeout=10))
    assert out.strip() == "ok"
