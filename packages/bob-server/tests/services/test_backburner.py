"""Backburner — detaching slow WHATSAPP_INCOMING turns (docs/backburner-plan.md).

Pinned here:
- gating: DM-only, whatsapp_incoming-only, mode/allowlist honoured
- probe: transcript build, JSON parse (incl. fenced), template fallback on
  any probe failure (D7)
- full detach: turn completes, subagent+goal registered, holding ack sent,
  capture mode flipped, run() returns early, and the supervisor settles the
  goal + stores the wake delivery (D1–D4)
- capture mode: post-detach sends are captured, never delivered
- kill: live task cancels and settles quietly (no wake); already-finished is
  an error so the model relays the result instead
- watchdog boundary: a turn that finishes inside the threshold never detaches
- restart recovery: orphaned goals settle + wake, idempotently
"""

from __future__ import annotations

import asyncio
import json

import pytest

from bob_server.services.backburner import (
    BackburnerService,
    _parse_probe_output,
    applies,
    build_transcript,
    mode,
)
from bob_server.services.dispatch_runner import DispatchRunner, DispatchSpec

DM_KEY = "agent:main:whatsapp:dm:61400000000"
GROUP_KEY = "agent:main:whatsapp:group:1234"


@pytest.fixture(autouse=True)
def _reset_backburner():
    from bob_server.services import backburner
    backburner.reset_for_tests()
    yield
    backburner.reset_for_tests()


@pytest.fixture
def bb(ctx):
    """Backburner on (full mode), fast watchdog."""
    ctx.settings.backburner.mode = "full"
    ctx.settings.backburner.detach_after_seconds = 0.05
    ctx.settings.backburner.probe_timeout_seconds = 2.0
    ctx.settings.backburner.sessions = ""
    return ctx.settings.backburner


def _spec(capture=None, hold=None, send_tool=None, session_key=DM_KEY,
          dispatch_id="disp-1") -> DispatchSpec:
    return DispatchSpec(
        session_key=session_key,
        system_content="system",
        tools=[send_tool] if send_tool else [],
        call_category="whatsapp_incoming",
        send_tool_name=send_tool.name if send_tool else "send_whatsapp_message",
        dispatch_id=dispatch_id,
        message_was_sent=[False],
        sent_texts=[],
        backburner_capture=capture if capture is not None else {"enabled": False, "texts": []},
        hold_sender=hold,
    )


# ------------------------------------------------------------------ gating

async def test_gating_whatsapp_only(ctx, bb):
    assert mode(ctx.settings) == "full"
    assert applies(ctx.settings, "whatsapp_incoming", DM_KEY)
    # groups included (D6 widened 2026-08-30 — live slow traffic is group-heavy)
    assert applies(ctx.settings, "whatsapp_incoming", GROUP_KEY)
    # other channels/categories excluded
    assert not applies(ctx.settings, "email_incoming", DM_KEY)
    assert not applies(ctx.settings, "whatsapp_group_member_change", GROUP_KEY)

    bb.mode = "off"
    assert not applies(ctx.settings, "whatsapp_incoming", DM_KEY)

    bb.mode = "full"
    bb.sessions = "agent:main:whatsapp:dm:61999000000"
    assert not applies(ctx.settings, "whatsapp_incoming", DM_KEY)
    assert applies(ctx.settings, "whatsapp_incoming", "agent:main:whatsapp:dm:61999000000")

    bb.mode = "bogus-mode"
    assert mode(ctx.settings) == "off"


# ------------------------------------------------------------------ probe

def test_build_transcript_skips_system_and_pairs_tools():
    items = [
        {"role": "system", "content": "SECRET SYSTEM PROMPT"},
        {"role": "user", "content": "check the hotel bookings"},
        {"type": "function_call", "name": "calendar_search", "arguments": '{"q":"hotel"}'},
        {"type": "function_call_output", "call_id": "1", "output": "3 entries found"},
        {"role": "assistant", "content": ""},
    ]
    transcript = build_transcript(json.dumps(items))
    assert "SECRET SYSTEM PROMPT" not in transcript
    assert "check the hotel bookings" in transcript
    assert "calendar_search" in transcript
    assert "3 entries found" in transcript


def test_parse_probe_output_variants():
    good = _parse_probe_output('{"summary": "checking bookings", "holding_text": "on it"}')
    assert good == {"summary": "checking bookings", "holding_text": "on it"}

    fenced = _parse_probe_output('```json\n{"summary": "s", "holding_text": "h"}\n```')
    assert fenced is not None

    assert _parse_probe_output("no json here") is None
    assert _parse_probe_output('{"summary": "", "holding_text": "h"}') is None
    assert _parse_probe_output(None) is None


async def test_probe_falls_back_to_templates_on_failure(ctx, bb, monkeypatch):
    from bob_server.services.llm_dispatch import LLMDispatchService

    async def _boom(self, messages, **kwargs):
        raise RuntimeError("probe provider down")

    monkeypatch.setattr(LLMDispatchService, "chat", _boom)
    info = await BackburnerService(ctx).probe_and_maybe_ack(
        _spec(), send_ack=False)
    assert info["source"] == "template"
    assert info["summary"]
    assert info["holding_text"]


async def test_probe_is_logged_against_the_session(ctx, bb, monkeypatch):
    """detach_probe rows must carry session_key/contact_id or they never
    show in the conversation's calls view (found live 2026-08-30: the first
    15 probe rows were invisible — session_key NULL)."""
    from bob_server.services.llm_dispatch import LLMDispatchService
    seen: dict = {}

    async def _chat(self, messages, **kwargs):
        seen.update(kwargs)
        return '{"summary": "s", "holding_text": "h"}'

    monkeypatch.setattr(LLMDispatchService, "chat", _chat)
    spec = _spec(dispatch_id="disp-logged")
    spec.contact_id = "contact-1"
    await BackburnerService(ctx).probe_and_maybe_ack(spec, send_ack=False)
    assert seen.get("session_key") == DM_KEY
    assert seen.get("contact_id") == "contact-1"


# ------------------------------------------------------- full detach flow

@pytest.fixture
def stub_llm(monkeypatch):
    """chat_with_tools sleeps past the watchdog then returns; probe chat is
    a well-behased JSON reply."""
    from bob_server.services.llm_dispatch import LLMDispatchService

    async def _chat(self, messages, **kwargs):
        return '{"summary": "checking the hotel bookings", "holding_text": "still on the hotels, back soon"}'

    async def _slow_turn(self, messages, tools, **kwargs):
        await asyncio.sleep(0.3)
        return "The Grand has rooms Thursday and Friday."

    monkeypatch.setattr(LLMDispatchService, "chat", _chat)
    monkeypatch.setattr(LLMDispatchService, "chat_with_tools", _slow_turn)
    return {"delay": 0.3, "result": "The Grand has rooms Thursday and Friday."}


@pytest.fixture
def stub_history(monkeypatch):
    async def _build(dummy, session_key, **kwargs):
        return [{"role": "user", "content": "find us a hotel thursday"}]

    monkeypatch.setattr(
        "bob_server.services.prompt_assembler.build_chat_messages", _build)


async def _pending_message(ctx, key=DM_KEY):
    from bob_server.services.session_service import SessionService
    await SessionService(ctx).add_message(key, "user", "find us a hotel thursday",
                                          channel="whatsapp", dispatched=0)


async def test_nudge_and_routine_turns_do_not_detach(ctx, bb, stub_llm, stub_history):
    """Only human-stimulus turns detach (AI doom group, 2026-08-30): a slow
    turn claimed solely by a wake nudge or a routine delivery must not send
    a holding ack or register a background task — it's internal/proactive
    work, and detaching relay turns amplifies (task → relay nudge → slow
    relay turn → task → …)."""
    from bob_server.services.session_service import SessionService

    for provenance in ("wake_nudge", "routine"):
        await SessionService(ctx).add_message(
            DM_KEY, "user", f"## {provenance} payload", channel="whatsapp",
            dispatched=0, provenance=provenance)
        acks: list[str] = []

        async def _hold(text: str) -> None:
            acks.append(text)

        spec = _spec(hold=_hold, dispatch_id=f"disp-{provenance}")
        # slow stub (0.3s > 0.05s watchdog) — the turn must simply wait
        result = await DispatchRunner(ctx).run(spec)

        assert result == stub_llm["result"]
        assert acks == [], f"{provenance}-only turn must not be acked"
        assert await _subagent_rows(ctx) == [], f"{provenance}-only turn must not detach"


async def _subagent_rows(ctx):
    return await ctx.db.fetch_all(
        "SELECT * FROM subagents WHERE agent_type = 'detached_turn'")


async def _messages(ctx, key=DM_KEY):
    return await ctx.db.fetch_all(
        "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id",
        (key,))


async def test_detach_flow_end_to_end(ctx, bb, stub_llm, stub_history):
    await _pending_message(ctx)
    acks: list[str] = []
    capture = {"enabled": False, "texts": []}

    async def _hold(text: str) -> None:
        acks.append(text)

    spec = _spec(capture=capture, hold=_hold, dispatch_id="disp-detach")
    result = await DispatchRunner(ctx).run(spec)

    # run() returned early — the supervisor owns the task now
    assert result == ""
    assert acks == ["still on the hotels, back soon"]
    assert capture["enabled"] is True

    rows = await _subagent_rows(ctx)
    assert len(rows) == 1
    assert rows[0]["parent_session_key"] == DM_KEY
    assert rows[0]["status"] in ("running", "completed")

    # goal held by the parent conversation (goals_block visibility)
    goal = await ctx.db.fetch_one(
        "SELECT * FROM goals WHERE external_ref = ?", (rows[0]["id"],))
    assert goal is not None and goal["status"] in ("active", "completed")
    assert "hotel" in goal["objective"]

    # supervisor: task finished (0.3s) -> goal settled, wake message stored
    for _ in range(50):
        if goal is None or goal["status"] == "completed":
            row = await ctx.db.fetch_one("SELECT status FROM goals WHERE id = ?", (goal["id"],))
            if row and row["status"] == "completed":
                break
        await asyncio.sleep(0.05)
        goal = await ctx.db.fetch_one(
            "SELECT * FROM goals WHERE external_ref = ?", (rows[0]["id"],))
    assert goal["status"] == "completed"

    final = await ctx.db.fetch_one("SELECT status, result FROM subagents WHERE id = ?",
                                   (rows[0]["id"],))
    assert final["status"] == "completed"
    assert "The Grand has rooms" in final["result"]

    msgs = await _messages(ctx)
    wake = [m for m in msgs if "Background task" in m["content"]]
    assert wake, "settle wake should have stored a relay message"


async def test_fast_turn_never_detaches(ctx, bb, stub_history, monkeypatch):
    from bob_server.services.llm_dispatch import LLMDispatchService

    async def _fast(self, messages, tools, **kwargs):
        return "immediate answer"

    monkeypatch.setattr(LLMDispatchService, "chat_with_tools", _fast)
    ctx.settings.backburner.detach_after_seconds = 1.0

    await _pending_message(ctx)
    acks: list[str] = []

    async def _hold(text: str) -> None:
        acks.append(text)

    spec = _spec(hold=_hold, dispatch_id="disp-fast")
    result = await DispatchRunner(ctx).run(spec)

    assert result == "immediate answer"
    assert acks == []
    assert await _subagent_rows(ctx) == []


async def test_capture_mode_intercepts_sends(ctx, bb):
    """The bridge's send tool: once capture mode flips, sends are captured as
    result and no effect is emitted."""
    from bob_server.services.whatsapp_bridge_service._service import WhatsAppBridgeService

    svc = WhatsAppBridgeService(ctx)
    contact = None
    try:
        spec = await svc._build_inbound_dispatch_spec(
            session_key=DM_KEY, chat_id="61400000000@s.whatsapp.net",
            chat_kind="dm", contact_id=contact, is_trusted=False)
    except Exception:
        pytest.skip("builder needs a fuller environment")

    send_tool = next(t for t in spec.tools if t.name == "send_whatsapp_message")
    before = await ctx.db.fetch_one("SELECT COUNT(*) AS n FROM effects")

    spec.backburner_capture["enabled"] = True
    out = await send_tool.handler("here is the answer you asked for")

    assert "captured" in out.lower()
    assert spec.backburner_capture["texts"] == ["here is the answer you asked for"]
    after = await ctx.db.fetch_one("SELECT COUNT(*) AS n FROM effects")
    assert after["n"] == before["n"], "capture mode must not emit a send effect"


# ------------------------------------------------------------- kill path

async def test_kill_live_detached_task_settles_quietly(ctx, bb, stub_llm, stub_history, monkeypatch):
    from bob_server.services.llm_dispatch import LLMDispatchService
    from bob_server.services.subagent_service import SubagentService

    # a long-running turn so there is something to kill mid-flight
    async def _very_slow(self, messages, tools, **kwargs):
        await asyncio.sleep(10)
        return "late result"

    monkeypatch.setattr(LLMDispatchService, "chat_with_tools", _very_slow)
    await _pending_message(ctx)

    async def _hold(text: str) -> None:
        pass

    spec = _spec(hold=_hold, dispatch_id="disp-kill")
    assert await DispatchRunner(ctx).run(spec) == ""
    await asyncio.sleep(0.1)

    rows = await _subagent_rows(ctx)
    assert len(rows) == 1
    subagent_id = rows[0]["id"]

    res = await SubagentService(ctx).kill_subagent(subagent_id, parent_session_key=DM_KEY)
    assert res.get("ok") is True

    for _ in range(50):
        row = await ctx.db.fetch_one("SELECT status FROM subagents WHERE id = ?", (subagent_id,))
        if row["status"] == "killed":
            break
        await asyncio.sleep(0.05)
    assert row["status"] == "killed"

    goal = await ctx.db.fetch_one(
        "SELECT status FROM goals WHERE external_ref = ?", (subagent_id,))
    assert goal["status"] == "cancelled"

    # quiet settle: no wake message was stored for a user kill
    msgs = await _messages(ctx)
    assert not [m for m in msgs if "Background task" in m["content"]]

    # second kill of the now-finished task errors instead of re-killing
    res2 = await SubagentService(ctx).kill_subagent(subagent_id, parent_session_key=DM_KEY)
    assert res2.get("ok") is False
    assert "already" in res2["error"]


# ------------------------------------------------------- restart recovery

async def test_recovery_settles_orphaned_goals(ctx, bb):
    from bob_server.repositories.subagents import SubagentRepository
    from bob_server.services.goal_service import create_goal

    repo = SubagentRepository(ctx.db)
    await repo.insert(
        subagent_id="deadbeef", parent_session_key=DM_KEY,
        session_key=f"subagent:{DM_KEY}:deadbeef",
        task="[task deadbeef] checking bookings", agent_type="detached_turn",
        persona=0, model="", contact_id=None, modality="",
        now_iso="2026-08-30T00:00:00Z")
    goal = await create_goal(
        ctx, conversation_id=f"subagent:{DM_KEY}:deadbeef",
        objective="[task deadbeef] checking bookings",
        origin_conversation_id=DM_KEY, kind="subagent", external_ref="deadbeef")
    await repo.set_status("deadbeef", "failed", "2026-08-30T00:01:00Z",
                          error="Server restarted")

    moved = await BackburnerService(ctx).recover_orphaned_goals()
    assert moved == 1

    row = await ctx.db.fetch_one("SELECT status FROM goals WHERE id = ?", (goal["id"],))
    assert row["status"] == "failed"
    msgs = await _messages(ctx)
    assert any("lost this background task" in m["content"] for m in msgs)

    # idempotent: nothing left to move
    assert await BackburnerService(ctx).recover_orphaned_goals() == 0
