"""Auto-handoff at the budget (2026-10-15 Bob cast).

A main conversational turn that exhausts its budget no longer just gets cut
off: its forced wrap-up round writes {"reply", "handoff"}, the reply goes to
the chat and the RUNNER (never the model) turns the brief into a goal room
before the turn ends — one worker, visible to the next turn here. Origin:
the Bob cast episode turn hit 300 s ten seconds before stitching its clips
and the group got nothing.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from server.services.dispatch_runner import _UNFINISHED_TURN_NOTICE, DispatchRunner
from server.services.openai_service import (
    _SELF_WRAP_FINAL_HANDOFF,
    _SELF_WRAP_NUDGE_HANDOFF,
    _SELF_WRAP_NUDGE_SEND,
    OpenAIService,
    parse_handoff_wrapup,
)
from server.services.session_service import SessionService
from tests.services.test_dispatch_rescue_gating import (  # noqa: F401  (fixtures)
    _FakeSendTool,
    _spec,
    stub_history,
)
from tests.test_self_wrap import FakeClock, _text_response, _tool_call_response

BRIEF = ("Request: 2-min Sarah-interviews-Bob AI headlines episode. Done: 12 clips in "
         "scratch/bobcast/. Left: ffmpeg concat → bobcast-news-ep1.mp3, post to the group.")


def test_parse_handoff_wrapup_shapes():
    assert parse_handoff_wrapup('{"reply": "on it", "handoff": "b"}') == ("on it", "b")
    assert parse_handoff_wrapup('```json\n{"reply": "r", "handoff": ""}\n```') == ("r", "")
    assert parse_handoff_wrapup("plain wrap-up text") is None
    assert parse_handoff_wrapup('{"handoff": "no reply"}') is None


@pytest.fixture
def wrap_client(monkeypatch):
    """Endless tool loop at 40 s/call; the tools-free forced round returns
    ``wrap["text"]`` and records its input."""
    wrap: dict = {"text": "", "input": None}
    clock = FakeClock()

    async def fake_create(**kwargs):
        if "tools" not in kwargs:
            wrap["input"] = list(kwargs.get("input", []))
            return _text_response(wrap["text"])
        clock.advance(40.0)
        return _tool_call_response()

    monkeypatch.setattr(OpenAIService, "client", property(
        lambda self: SimpleNamespace(responses=SimpleNamespace(create=fake_create))))
    import server.services.openai_service as svc_mod
    monkeypatch.setattr(svc_mod, "time", SimpleNamespace(monotonic=clock.monotonic))
    return wrap


async def _run(ctx, **kw):
    async def handler(**_):
        return "tool result"
    return await OpenAIService(ctx).run_turn(
        [{"role": "user", "content": "make the episode"}], [], {"probe_tool": handler},
        model="gpt-5.6-sol", time_limit_seconds=100, **kw)


async def test_budget_wrapup_returns_reply_and_brief(ctx, wrap_client):
    wrap_client["text"] = json.dumps({"reply": "clips done, stitching — will post here",
                                      "handoff": BRIEF})
    out: dict[str, str] = {}
    assert await _run(ctx, handoff_out=out) == "clips done, stitching — will post here"
    assert out == {"brief": BRIEF}
    assert any(m.get("content") == _SELF_WRAP_FINAL_HANDOFF
               for m in wrap_client["input"] if isinstance(m, dict))


async def test_unparseable_wrapup_falls_back_to_plain_text(ctx, wrap_client):
    wrap_client["text"] = "here is what I have so far"
    out: dict[str, str] = {}
    assert await _run(ctx, handoff_out=out) == "here is what I have so far"
    assert out == {}


async def test_without_handoff_out_the_old_wrapup_prompt_is_used(ctx, wrap_client):
    wrap_client["text"] = "summary"
    assert await _run(ctx) == "summary"
    assert not any(m.get("content") == _SELF_WRAP_FINAL_HANDOFF
                   for m in wrap_client["input"] if isinstance(m, dict))


# ---------------------------------------------------------------- runner

@pytest.fixture
def handoff_llm(monkeypatch):
    """run_turn emulating a budget-exhausted turn: fills handoff_out."""
    from server.services.llm_dispatch import LLMDispatchService
    seen: dict = {"reply": "clips done, stitching — will post here", "brief": BRIEF}

    async def _run_turn(self, messages, tools, **kwargs):
        seen["handoff_out_passed"] = kwargs.get("handoff_out") is not None
        if kwargs.get("handoff_out") is not None and seen["brief"]:
            kwargs["handoff_out"]["brief"] = seen["brief"]
        return seen["reply"]

    monkeypatch.setattr(LLMDispatchService, "run_turn", _run_turn)
    return seen


async def _goals_from(db, key: str) -> list:
    return await db.fetch_all(
        "SELECT * FROM goals WHERE origin_conversation_id = ? OR conversation_id = ?",
        (key, key))


async def test_runner_creates_handoff_goal_and_delivers_reply(
        ctx, db, handoff_llm, stub_history):
    key = "test:handoff:create"
    await SessionService(ctx).add_message(key, "user", "make the episode", dispatched=0)
    send_tool = _FakeSendTool("send_whatsapp_message")

    await DispatchRunner(ctx).run(_spec(key, send_tool))

    goals = await _goals_from(db, key)
    assert len(goals) == 1
    assert goals[0]["kind"] == "handoff" and goals[0]["status"] == "active"
    assert BRIEF in goals[0]["objective"]
    assert send_tool.delivered == ["clips done, stitching — will post here"]


async def test_goal_opened_this_turn_gets_the_brief_not_a_twin(
        ctx, db, handoff_llm, stub_history, monkeypatch):
    """The model took the 60 s nudge and called add_goal itself."""
    from server.services.goal_service import create_goal
    from server.services.llm_dispatch import LLMDispatchService

    key = "test:handoff:dedupe"
    await SessionService(ctx).add_message(key, "user", "make the episode", dispatched=0)
    made: dict = {}

    async def _run_turn(self, messages, tools, **kwargs):
        made["goal"] = await create_goal(ctx, conversation_id=key,
                                         objective="Bobcast episode", profile="outcome")
        kwargs["handoff_out"]["brief"] = BRIEF
        return "on it"

    monkeypatch.setattr(LLMDispatchService, "run_turn", _run_turn)
    await DispatchRunner(ctx).run(_spec(key, _FakeSendTool("send_whatsapp_message")))

    goals = await _goals_from(db, key)
    assert [g["id"] for g in goals] == [made["goal"]["id"]]
    row = await db.fetch_one("SELECT strategy_json FROM goals WHERE id = ?",
                             (made["goal"]["id"],))
    assert "Handoff brief" in (row["strategy_json"] or "")


async def test_failed_goal_create_sends_unfinished_notice(
        ctx, db, handoff_llm, stub_history, monkeypatch):
    async def _fail(*a, **kw):
        return {"ok": False, "error": "boom"}

    monkeypatch.setattr("server.services.effects.emit_and_deliver", _fail)
    key = "test:handoff:fail"
    await SessionService(ctx).add_message(key, "user", "make the episode", dispatched=0)
    send_tool = _FakeSendTool("send_whatsapp_message")

    await DispatchRunner(ctx).run(_spec(key, send_tool))

    assert send_tool.delivered == [_UNFINISHED_TURN_NOTICE]


@pytest.mark.parametrize("env,mode", [("off", "hold"), ("on", "full")])
async def test_kill_switch_and_full_mode_skip_handoff(
        ctx, db, handoff_llm, stub_history, monkeypatch, env, mode):
    monkeypatch.setenv("BOB_AUTO_HANDOFF", env)
    ctx.settings.backburner.mode = mode
    key = f"test:handoff:{env}-{mode}"
    await SessionService(ctx).add_message(key, "user", "make the episode", dispatched=0)

    await DispatchRunner(ctx).run(_spec(key, _FakeSendTool("send_whatsapp_message")))

    assert handoff_llm["handoff_out_passed"] is False
    assert await _goals_from(db, key) == []


async def test_main_turn_soft_nudge_points_at_goals_not_self_promises(ctx, wrap_client):
    """2026-10-16: the send-variant nudge told Luna to park the unfinished
    Bobcast stitch as a promise to itself (due an hour later)."""
    wrap_client["text"] = json.dumps({"reply": "r", "handoff": ""})
    await _run(ctx, handoff_out={})
    msgs = [m.get("content") for m in wrap_client["input"] if isinstance(m, dict)]
    assert _SELF_WRAP_NUDGE_HANDOFF in msgs and _SELF_WRAP_NUDGE_SEND not in msgs
    assert "add_goal" in _SELF_WRAP_NUDGE_HANDOFF
    assert "profile='promise'" not in _SELF_WRAP_NUDGE_HANDOFF
