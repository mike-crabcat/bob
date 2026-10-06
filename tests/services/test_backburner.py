"""Backburner — detaching slow WHATSAPP_INCOMING turns (docs/detach-v2.md).

Pinned here:
- gating: whatsapp_incoming-only, mode/allowlist honoured
- probe: transcript build, JSON parse (incl. fenced), template fallback on
  any probe failure (D7)
- full detach (v2): turn completes, subagent+goal registered, holding ack
  sent, transcript placeholder written, flight dict armed (attribution, no
  suppression), run() returns early, supervisor settles
- v2 delivery: post-detach sends DELIVER and are attributed (provenance
  bg_send + task id); a flight that spoke settles quietly; a silent flight
  gets the non-imperative fallback wake (task_relay, rescue-covered)
- kill: live task cancels and settles quietly (no wake); already-finished is
  an error so the model reports the result instead
- watchdog boundary: a turn that finishes inside the threshold never detaches;
  a turn that speaks or finishes while the detach probe runs never detaches
  either (the 2026-08-31 AI doom duplicate)
- restart recovery: orphaned goals settle + wake, idempotently
"""

from __future__ import annotations

import asyncio
import json

import pytest

from server.services.backburner import (
    BackburnerService,
    _parse_probe_output,
    applies,
    build_transcript,
    mode,
)
from server.services.dispatch_runner import DispatchRunner, DispatchSpec

DM_KEY = "agent:main:whatsapp:dm:61400000000"
GROUP_KEY = "agent:main:whatsapp:group:1234"


@pytest.fixture(autouse=True)
def _reset_backburner():
    from server.services import backburner
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


def _spec(flight=None, hold=None, send_tool=None, session_key=DM_KEY,
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
        flight=flight if flight is not None else {},
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


def test_build_transcript_ignores_prior_turn_history():
    """Live 2026-08-30: a group turn 32s in with zero tool calls of its own
    showed the probe a full tail of the previous turns' work (merg ledger
    checks, the 257 answer) — the probe nearly summarised the wrong turn.
    History chat items AND history tool items before the last user message
    must be excluded."""
    items = [
        {"role": "system", "content": "SECRET SYSTEM PROMPT"},
        {"role": "user", "content": "[Mike] what's a 257?"},
        {"type": "function_call", "name": "search_notes", "arguments": '{"q":"257"}'},
        {"type": "function_call_output", "call_id": "1", "output": "no marker found"},
        {"role": "assistant", "content": "It's in none of my books."},
        {"role": "user", "content": "[Sylvain] I need to change my symbolic number"},
    ]
    transcript = build_transcript(json.dumps(items))
    assert "symbolic number" in transcript
    assert "257" not in transcript            # prior turn's trigger
    assert "search_notes" not in transcript   # prior turn's tool work
    assert "none of my books" not in transcript
    assert "still on the first response" in transcript  # own work: none yet


def test_build_transcript_no_user_item_returns_empty():
    """Unexpected shape with no user message at all — no reliable boundary,
    so show nothing (run_probe falls back to its generic placeholder)."""
    items = [
        {"role": "system", "content": "SECRET SYSTEM PROMPT"},
        {"type": "function_call", "name": "x", "arguments": "{}"},
    ]
    assert build_transcript(json.dumps(items)) == ""


async def _seed_running_call(ctx, dispatch_id: str, messages_json: str) -> None:
    await ctx.db.execute(
        """INSERT INTO llm_call_log (id, provider, call_category, dispatch_id,
           messages_json, status) VALUES (?, 'openai', 'whatsapp_incoming',
           ?, ?, 'running')""",
        (f"log-{dispatch_id}", dispatch_id, messages_json))


async def _seed_active_turn(ctx, dispatch_id: str, trigger: str = "check the hotel bookings") -> None:
    """Seed a running llm_call_log row WITH tool activity — the probe's
    evidence gate (2026-10-04) skips the probe LLM entirely for zero-tool
    turns, so tests exercising the probe path must show observable work."""
    await _seed_running_call(ctx, dispatch_id, json.dumps([
        {"role": "user", "content": trigger},
        {"type": "function_call", "name": "calendar_search", "call_id": "c1",
         "arguments": '{"q":"hotel"}'},
        {"type": "function_call_output", "call_id": "c1", "output": "3 entries"},
    ]))


@pytest.mark.asyncio
async def test_run_probe_zero_tool_activity_neutral_no_llm(ctx, monkeypatch):
    """Evidence gate (2026-10-03 incident: 'searching messages and memory…'
    ack from a flight that finished 2s later having called nothing): a turn
    with zero tool calls gets the neutral template and the probe LLM is
    never even called — nothing observable to summarize."""
    from server.services import backburner as bb
    from server.services.llm_dispatch import LLMDispatchService

    async def _must_not_run(self, *a, **kw):  # type: ignore[no-untyped-def]
        raise AssertionError("probe LLM must not be called with zero tool activity")

    monkeypatch.setattr(LLMDispatchService, "prompt", _must_not_run)
    await _seed_running_call(ctx, "d-zero", json.dumps([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "what band is playing in freo tonight?"},
    ]))

    info = await bb.run_probe(ctx, "d-zero")
    assert info["source"] == "no_activity"
    assert "no tool activity" in info["summary"]
    assert info["holding_text"] == bb.TEMPLATE_HOLDING
    assert "search" not in info["summary"].lower(), (
        "must not claim any action")


@pytest.mark.asyncio
async def test_run_probe_prompt_carries_called_tools(ctx, monkeypatch):
    """With real tool activity the probe still runs, but the prompt states
    the evidence contract: the called tools by name, and a ban on inferring
    or inventing actions beyond them (option 2)."""
    from server.services import backburner as bb
    from server.services.llm_dispatch import LLMDispatchService

    seen: dict = {}

    async def _fake_chat(self, messages, **kw):  # type: ignore[no-untyped-def]
        seen["system"] = messages[0]["content"]
        return '{"summary": "checking the calendar", "holding_text": "on it"}'

    monkeypatch.setattr(LLMDispatchService, "prompt", _fake_chat)
    await _seed_running_call(ctx, "d-tools", json.dumps([
        {"role": "user", "content": "check the hotel bookings"},
        {"type": "function_call", "name": "calendar_search", "call_id": "1",
         "arguments": '{"q":"hotel"}'},
        {"type": "function_call_output", "call_id": "1", "output": "3 entries"},
        {"type": "function_call", "name": "calendar_search", "call_id": "2",
         "arguments": '{"q":"hotel2"}'},
    ]))

    info = await bb.run_probe(ctx, "d-tools")
    assert info["summary"] == "checking the calendar"
    assert "calendar_search" in seen["system"], "called tools listed as evidence"
    assert "ONLY" in seen["system"] and "Never infer" in seen["system"]


def test_parse_probe_output_variants():
    good = _parse_probe_output('{"summary": "checking bookings", "holding_text": "on it"}')
    assert good == {"summary": "checking bookings", "holding_text": "on it"}

    fenced = _parse_probe_output('```json\n{"summary": "s", "holding_text": "h"}\n```')
    assert fenced is not None

    assert _parse_probe_output("no json here") is None
    assert _parse_probe_output('{"summary": "", "holding_text": "h"}') is None
    assert _parse_probe_output(None) is None


async def test_probe_falls_back_to_templates_on_failure(ctx, bb, monkeypatch):
    from server.services.llm_dispatch import LLMDispatchService

    async def _boom(self, messages, **kwargs):
        raise RuntimeError("probe provider down")

    monkeypatch.setattr(LLMDispatchService, "prompt", _boom)
    await _seed_active_turn(ctx, "disp-1")
    info = await BackburnerService(ctx).probe_and_maybe_ack(
        _spec(), send_ack=False)
    assert info["source"] == "template"
    assert info["summary"]
    assert info["holding_text"]


async def test_probe_is_logged_against_the_session(ctx, bb, monkeypatch):
    """detach_probe rows must carry session_key/contact_id or they never
    show in the conversation's calls view (found live 2026-08-30: the first
    15 probe rows were invisible — session_key NULL)."""
    from server.services.llm_dispatch import LLMDispatchService
    seen: dict = {}

    async def _chat(self, messages, **kwargs):
        seen.update(kwargs)
        return '{"summary": "s", "holding_text": "h"}'

    monkeypatch.setattr(LLMDispatchService, "prompt", _chat)
    await _seed_active_turn(ctx, "disp-logged")
    spec = _spec(dispatch_id="disp-logged")
    spec.contact_id = "contact-1"
    await BackburnerService(ctx).probe_and_maybe_ack(spec, send_ack=False)
    assert seen.get("session_key") == DM_KEY
    assert seen.get("contact_id") == "contact-1"


# ------------------------------------------------------- full detach flow

@pytest.fixture
def stub_llm(monkeypatch):
    """run_turn sleeps past the watchdog then returns; probe chat is
    a well-behased JSON reply."""
    from server.services.llm_dispatch import LLMDispatchService

    async def _chat(self, messages, **kwargs):
        return '{"summary": "checking the hotel bookings", "holding_text": "still on the hotels, back soon"}'

    async def _slow_turn(self, messages, tools, **kwargs):
        await asyncio.sleep(0.3)
        return "The Grand has rooms Thursday and Friday."

    monkeypatch.setattr(LLMDispatchService, "prompt", _chat)
    monkeypatch.setattr(LLMDispatchService, "run_turn", _slow_turn)
    return {"delay": 0.3, "result": "The Grand has rooms Thursday and Friday."}


@pytest.fixture
def stub_history(monkeypatch):
    async def _build(dummy, session_key, **kwargs):
        return [{"role": "user", "content": "find us a hotel thursday"}]

    monkeypatch.setattr(
        "server.services.prompt_assembler.build_chat_messages", _build)


async def _pending_message(ctx, key=DM_KEY):
    from server.services.session_service import SessionService
    await SessionService(ctx).add_message(key, "user", "find us a hotel thursday",
                                          channel="whatsapp", dispatched=0)


async def test_nudge_and_routine_turns_do_not_detach(ctx, bb, stub_llm, stub_history):
    """Only human-stimulus turns detach (AI doom group, 2026-08-30): a slow
    turn claimed solely by a wake nudge, a routine delivery, or a background
    task relay must not send a holding ack or register a background task —
    it's internal/proactive work, and detaching relay turns amplifies
    (task → relay nudge → slow relay turn → task → …). task_relay is
    rescue-eligible but still detach-quiet (dispatch_runner set split)."""
    from server.services.session_service import SessionService

    for provenance in ("wake_nudge", "routine", "task_relay"):
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
    """Detached flights — runs since 2026-10-05 (commitments Phase 0)."""
    return await ctx.db.fetch_all("SELECT * FROM runs WHERE kind = 'flight'")


async def _messages(ctx, key=DM_KEY):
    return await ctx.db.fetch_all(
        "SELECT role, content, provenance FROM messages "
        "WHERE conversation_id = ? ORDER BY id",
        (key,))


async def test_detach_flow_end_to_end(ctx, bb, stub_llm, stub_history):
    from server.services.tools import Tool

    await _pending_message(ctx)
    await _seed_active_turn(ctx, "disp-detach")
    acks: list[str] = []
    flight: dict = {}
    sends: list[str] = []

    async def _hold(text: str) -> None:
        acks.append(text)

    async def _send(text: str = "", media_path: str = "") -> str:
        sends.append(text)
        return "Message sent (request_id=test)"

    send_tool = Tool(name="send_whatsapp_message", description="send",
                     parameters={}, required=[], handler=_send)
    spec = _spec(flight=flight, hold=_hold, send_tool=send_tool,
                 dispatch_id="disp-detach")
    result = await DispatchRunner(ctx).run(spec)

    # run() returned early — the supervisor owns the task now
    assert result == ""
    assert acks == ["still on the hotels, back soon"]
    assert flight.get("subagent_id"), "flight must be armed at detach"

    rows = await _subagent_rows(ctx)
    assert len(rows) == 1
    assert rows[0]["session_key"] == DM_KEY
    assert rows[0]["status"] in ("running", "completed")

    # v2: the transcript placeholder announces the flight
    msgs = await _messages(ctx)
    placeholder = [m for m in msgs if m["provenance"] == "bg_placeholder"]
    assert placeholder and "detached" in placeholder[0]["content"]
    assert flight["subagent_id"][:8] in placeholder[0]["content"]

    # Phase 0: a flight is execution, not intent — no goal, no subagent row
    assert await ctx.db.fetch_all(
        "SELECT id FROM goals WHERE external_ref = ?", (rows[0]["id"],)) == []
    assert await ctx.db.fetch_all(
        "SELECT id FROM subagents WHERE id = ?", (rows[0]["id"],)) == []

    # supervisor: task finished (0.3s) -> run completed. The stub never
    # calls the send tool (silent flight) -> final-text delivery: the
    # supervisor delivers the result through the send tool itself.
    for _ in range(50):
        final = await ctx.db.fetch_one("SELECT status, result FROM runs WHERE id = ?",
                                       (rows[0]["id"],))
        if final["status"] == "completed":
            break
        await asyncio.sleep(0.05)
    assert final["status"] == "completed"
    assert "The Grand has rooms" in final["result"]

    assert sends, "silent flight's result must be delivered at terminal"
    assert "The Grand has rooms" in sends[0]
    assert not sends[0].startswith("(background result"), (
        "no bg-turn header — verbatim delivery (Mike 2026-10-03). The "
        "UNVERIFIED marker may still lead (this fixture records no tool "
        "calls, so the honesty ledger fires)")
    msgs = await _messages(ctx)
    assert not [m for m in msgs if m["provenance"] == "task_relay"], (
        "terminal delivery replaces the relay wake (2026-10-01)")


async def test_fast_turn_never_detaches(ctx, bb, stub_history, monkeypatch):
    from server.services.llm_dispatch import LLMDispatchService

    async def _fast(self, messages, tools, **kwargs):
        return "immediate answer"

    monkeypatch.setattr(LLMDispatchService, "run_turn", _fast)
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


# ------------------------------------------------- the mid-probe race

async def test_turn_that_spokes_during_probe_never_detaches(ctx, bb, stub_history, monkeypatch):
    """Live 2026-08-31, AI doom group: the turn crossed the 30s threshold,
    its send landed while the detach probe was in flight, and detach
    proceeded anyway — _terminal then relayed the already-delivered reply
    back as a speak-expected task_relay and the group got a duplicate. A
    turn that already spoke must fall through to the inline wait."""
    from server.services.llm_dispatch import LLMDispatchService

    FINAL = "Scanned it. The profile's history holds about 21 distinct hosts."

    async def _slow_probe(self, messages, **kwargs):
        await asyncio.sleep(0.1)   # probe in flight while the send lands
        return '{"summary": "scanning browser history", "holding_text": "still counting"}'

    sent_flag = [False]
    sent_texts: list[str] = []

    async def _racing_turn(self, messages, tools, **kwargs):
        await asyncio.sleep(0.08)  # watchdog (0.05s) fires, probe starts…
        sent_flag[0] = True        # …then the send lands mid-probe
        sent_texts.append(FINAL)
        await asyncio.sleep(0.1)
        return FINAL

    monkeypatch.setattr(LLMDispatchService, "prompt", _slow_probe)
    monkeypatch.setattr(LLMDispatchService, "run_turn", _racing_turn)

    await _pending_message(ctx)
    await _seed_active_turn(ctx, "disp-race")
    acks: list[str] = []
    flight: dict = {}

    async def _hold(text: str) -> None:
        acks.append(text)

    spec = _spec(flight=flight, hold=_hold, dispatch_id="disp-race")
    spec.message_was_sent = sent_flag
    spec.sent_texts = sent_texts
    result = await DispatchRunner(ctx).run(spec)

    assert result == FINAL, "run() must wait inline, not return early on detach"
    assert acks == []
    assert "subagent_id" not in flight, "no flight armed — no detach happened"
    assert await _subagent_rows(ctx) == []
    msgs = await _messages(ctx)
    assert not [m for m in msgs if m["provenance"] in ("bg_placeholder", "task_relay")], (
        "an already-delivered result must not be woken back")


async def test_task_finishing_during_probe_never_detaches(ctx, bb, monkeypatch):
    """The other half of the guard: the llm task completing while the probe
    runs is the same race with the flag never flipped — detach must abort
    before registering anything."""
    from server.services.llm_dispatch import LLMDispatchService

    async def _slow_probe(self, messages, **kwargs):
        await asyncio.sleep(0.1)
        return '{"summary": "s", "holding_text": "h"}'

    monkeypatch.setattr(LLMDispatchService, "prompt", _slow_probe)

    done_task = asyncio.create_task(asyncio.sleep(0))
    await done_task
    spec = _spec(dispatch_id="disp-done")
    assert await BackburnerService(ctx).detach(
        spec=spec, turn=None, session_svc=None, llm_task=done_task) is False
    assert await _subagent_rows(ctx) == []


async def test_post_detach_send_delivers_attributed(ctx, bb):
    """v2: a flight's send DELIVERS (real effect emitted) and is attributed —
    history row with provenance bg_send + the task id, tee kept for the
    terminal audit, tool response carries the background regime note. The
    v1 capture branch (suppression + the relay lie) is gone."""
    from server.services.whatsapp_bridge_service._service import WhatsAppBridgeService

    svc = WhatsAppBridgeService(ctx)
    contact = None
    try:
        spec = await svc._build_inbound_dispatch_spec(
            session_key=DM_KEY, chat_id="61400000000@s.whatsapp.net",
            chat_kind="dm", contact_id=contact, is_trusted=False,
            human_initiated=False)
    except Exception:
        pytest.skip("builder needs a fuller environment")

    send_tool = next(t for t in spec.tools if t.name == "send_whatsapp_message")
    before = await ctx.db.fetch_one("SELECT COUNT(*) AS n FROM effects")

    spec.flight["subagent_id"] = "1234567890abcdef"
    spec.flight["sent"] = False
    # 2026-09-17 token: this test runs in the LLM task's place — arm the
    # flight the way detach() does, with the running task as the token.
    spec.flight["detach_task"] = asyncio.current_task()
    out = await send_tool.handler("here is the answer you asked for")

    assert "attributed to background turn" in out, out
    assert "detached background turn" in out, "the regime note must ride the response"
    after = await ctx.db.fetch_one("SELECT COUNT(*) AS n FROM effects")
    assert after["n"] == before["n"] + 1, "the flight's send must actually deliver"
    assert spec.flight["sent"] is True
    assert spec.flight["texts"] == ["here is the answer you asked for"]
    row = await ctx.db.fetch_one(
        "SELECT provenance, metadata FROM messages "
        "WHERE conversation_id = ? AND provenance = 'bg_send' "
        "ORDER BY created_at DESC LIMIT 1", (DM_KEY,))
    assert row is not None, "attributed history row must be written"
    assert "12345678" in (row["metadata"] or "")


async def test_reflown_spec_keeps_the_live_voice(ctx, bb):
    """2026-09-16 AI doom incident: the attention coordinator's leftover
    sweep re-armed the FLOWN spec for messages that arrived mid-turn — after
    that flight detached, the re-run's sends were stamped bg_send under the
    old task id. The token check must return the live voice: same flight
    dict, different running task."""
    from server.services.whatsapp_bridge_service._service import WhatsAppBridgeService

    svc = WhatsAppBridgeService(ctx)
    try:
        spec = await svc._build_inbound_dispatch_spec(
            session_key=DM_KEY, chat_id="61400000000@s.whatsapp.net",
            chat_kind="dm", contact_id=None, is_trusted=False,
            human_initiated=False)
    except Exception:
        pytest.skip("builder needs a fuller environment")

    send_tool = next(t for t in spec.tools if t.name == "send_whatsapp_message")
    other_task = asyncio.ensure_future(asyncio.sleep(0))
    try:
        spec.flight["subagent_id"] = "1234567890abcdef"
        spec.flight["sent"] = False
        spec.flight["detach_task"] = other_task  # the detached task, not us

        out = await send_tool.handler("live voice reply")

        assert "attributed to background turn" not in out, out
        assert spec.flight["sent"] is False, "must not mark the flight as spoken"
        row = await ctx.db.fetch_one(
            "SELECT COUNT(*) AS n FROM messages "
            "WHERE conversation_id = ? AND provenance = 'bg_send'", (DM_KEY,))
        assert row["n"] == 0, "re-flown turn's sends must not be bg-attributed"
    finally:
        await other_task


def test_active_bg_id_token_rules():
    """active_bg_id: no flight / no id → None; token match → id; token
    mismatch → None; missing token (pre-deploy flight) → old behaviour."""
    import asyncio
    from server.services.backburner import active_bg_id

    assert active_bg_id(None) is None
    assert active_bg_id({}) is None
    assert active_bg_id({"sent": False}) is None

    async def _case():
        me = asyncio.current_task()
        assert active_bg_id({"subagent_id": "abc"}) == "abc", (
            "pre-deploy flights without a token keep the old behaviour")
        assert active_bg_id({"subagent_id": "abc", "detach_task": me}) == "abc"
        other = asyncio.ensure_future(asyncio.sleep(0))
        try:
            assert active_bg_id(
                {"subagent_id": "abc", "detach_task": other}) is None
        finally:
            await other

    asyncio.run(_case())


def test_spec_detached_predicate():
    from server.services.backburner import spec_detached

    assert spec_detached(None) is False
    assert spec_detached(type("S", (), {"flight": {}})()) is False
    assert spec_detached(type("S", (), {"flight": None})()) is False
    assert spec_detached(
        type("S", (), {"flight": {"subagent_id": "x"}})()) is True


# ------------------------------------------------- _terminal (v2)

async def _settled_detached_task(ctx, *, flight: dict, result_text: str,
                                 status: str = "completed",
                                 sends: list | None = None,
                                 steer_origin: bool = False):
    """Register a flight run the way detach() does, run _terminal on it,
    and return the run row. Pass ``sends`` (a list) to arm a recording send
    tool so terminal delivery has somewhere to go; without it the spec
    carries no send tool and terminal paths fall back to stored-only."""
    from server.repositories.runs import RunRepository
    from server.services.tools import Tool

    subagent_id = "aaaabbbb"
    await RunRepository(ctx.db).start(
        run_id=subagent_id, kind="flight", session_key=DM_KEY,
        summary="scanning browser history",
        metadata={"steer_origin": True} if steer_origin else None,
        now_iso="2026-08-31T00:00:00Z")

    send_tool = None
    if sends is not None:
        async def _send(text: str = "", media_path: str = "") -> str:
            sends.append(text)
            return "Message sent (request_id=test)"

        send_tool = Tool(name="send_whatsapp_message",
                         description="send", parameters={}, required=[],
                         handler=_send)

    spec = _spec(flight=flight, send_tool=send_tool)
    await BackburnerService(ctx)._terminal(
        subagent_id, spec, status=status, result_text=result_text)
    return await RunRepository(ctx.db).get(subagent_id)


async def test_terminal_flight_that_spoke_settles_quietly(ctx, bb):
    """v2 core invariant: the flight's attributed messages WERE the output —
    no wake, no relay turn, nothing to re-deliver (this is the fix for the
    2026-09-10 double-sell and 2026-09-11 double-gif)."""
    goal = await _settled_detached_task(
        ctx,
        flight={"subagent_id": "aaaabbbb", "sent": True,
                "texts": ["The Grand has rooms"]},
        result_text="done: checked the hotels")

    row = await ctx.db.fetch_one("SELECT status FROM runs WHERE id = ?", (goal["id"],))
    assert row["status"] == "completed"
    msgs = await _messages(ctx)
    assert not [m for m in msgs if m["provenance"] == "task_relay"], (
        "a flight that spoke must never spawn a delivery turn"
        " — that instruction was the v1 duplicate mechanism")


async def test_terminal_silent_flight_result_delivered(ctx, bb):
    """Final-text delivery (2026-10-01): a silent completion with a result
    is delivered DIRECTLY by the supervisor through the send tool — no
    relay turn (the 2026-09-29 send-skip class cost a full relay + dead-man
    rescue per incident). Verbatim, no header (Mike 2026-10-03: a detached
    turn is still a reply to whoever asked). Flight ran tools, so the
    result is vouched as real work."""
    sends: list[str] = []
    goal = await _settled_detached_task(
        ctx, flight={"subagent_id": "aaaabbbb", "sent": False, "texts": [],
                     "tool_calls": 3},
        result_text="Scanned it. The profile's history holds about 21 distinct hosts.",
        sends=sends)

    row = await ctx.db.fetch_one("SELECT status FROM runs WHERE id = ?", (goal["id"],))
    assert row["status"] == "completed"
    assert sends, "silent flight with a result must deliver it at terminal"
    assert sends[0] == "Scanned it. The profile's history holds about 21 distinct hosts."
    msgs = await _messages(ctx)
    assert not [m for m in msgs if m["provenance"] == "task_relay"], (
        "terminal delivery replaces the relay wake")
    rows = [m for m in msgs if m["role"] == "assistant"
            and "21 distinct hosts" in (m["content"] or "")]
    assert rows, "terminal delivery must record an assistant history row"
    assert sends[0] in rows[0]["content"]


async def test_terminal_zero_tool_flight_delivers_unverified(ctx, bb):
    """The 2026-09-17 phantom build: a silent flight with ZERO tool calls
    claiming 'Build is running'. Still delivered at terminal (rare, and
    usually answer-shaped) but prefixed with the UNVERIFIED marker that
    withdraws the vouch — the harm was the system asserting work happened;
    the marker says it may not have, and that it still needs doing. (The
    only framing that survives the 2026-10-03 verbatim ruling.)"""
    sends: list[str] = []
    goal = await _settled_detached_task(
        ctx, flight={"subagent_id": "aaaabbbb", "sent": False, "texts": [],
                     "tool_calls": 0},
        result_text="Build is running — mixed when it lands. "
                    "I'll post the file when it's done.",
        sends=sends)

    assert sends, "zero-call flights still surface — loudly framed, never silently"
    assert "UNVERIFIED" in sends[0]
    assert "ran no tools" in sends[0]
    assert "still needs doing" in sends[0]
    assert "Build is running" in sends[0]
    msgs = await _messages(ctx)
    assert [m for m in msgs if m["role"] == "assistant"
            and "Build is running" in (m["content"] or "")], (
        "unverified terminal delivery must record a history row too")


def test_note_tool_call_counts_only_armed_flights():
    from server.services import backburner as bb

    bb.reset_for_tests()
    flight: dict = {"tool_calls": 0}
    bb._flight_by_dispatch["d-1"] = flight
    bb.note_tool_call("d-1")
    bb.note_tool_call("d-1")
    bb.note_tool_call("d-unarmed")     # no registration — no-op
    bb.note_tool_call(None)
    assert flight["tool_calls"] == 2
    bb.reset_for_tests()


async def test_terminal_failed_flight_wakes_honestly(ctx, bb):
    goal = await _settled_detached_task(
        ctx, flight={"subagent_id": "aaaabbbb", "sent": False, "texts": []},
        result_text="the upstream API 500'd twice", status="failed")

    msgs = await _messages(ctx)
    wake = [m for m in msgs if m["provenance"] == "task_relay"]
    assert wake and "failed" in wake[0]["content"]
    assert "may have had real effects" in wake[0]["content"]


# ------------------------------------------------------------- kill path

async def test_kill_live_detached_task_settles_quietly(ctx, bb, stub_llm, stub_history, monkeypatch):
    from server.services.llm_dispatch import LLMDispatchService
    from server.services.subagent_service import SubagentService

    # a long-running turn so there is something to kill mid-flight
    async def _very_slow(self, messages, tools, **kwargs):
        await asyncio.sleep(10)
        return "late result"

    monkeypatch.setattr(LLMDispatchService, "run_turn", _very_slow)
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
        row = await ctx.db.fetch_one("SELECT status FROM runs WHERE id = ?", (subagent_id,))
        if row["status"] == "killed":
            break
        await asyncio.sleep(0.05)
    assert row["status"] == "killed"

    # quiet settle: no wake message was stored for a user kill
    msgs = await _messages(ctx)
    assert not [m for m in msgs if "Background task" in m["content"]]

    # second kill of the now-finished task errors instead of re-killing
    res2 = await SubagentService(ctx).kill_subagent(subagent_id, parent_session_key=DM_KEY)
    assert res2.get("ok") is False
    assert "already" in res2["error"]


# ------------------------------------------------------- restart recovery

# ------------------------------------------- silent steer turns (2026-09-06)

async def test_steer_only_turn_detaches_silently(ctx, bb, stub_llm, stub_history):
    """A slow steer-only turn still detaches (lock frees, work continues in
    the background) but sends NO holding ack — nobody asked anything. v2:
    a silent steer-born flight settles QUIETLY (silence is the designed
    outcome for stimulus work — no fallback wake, nobody to report to)."""
    from server.services.session_service import SessionService

    await SessionService(ctx).add_message(
        DM_KEY, "user", "[Stimulus: frigate activity.person] person at doorbell",
        channel="whatsapp", dispatched=0, provenance="steer")
    acks: list[str] = []
    flight: dict = {}

    async def _hold(text: str) -> None:
        acks.append(text)

    spec = _spec(flight=flight, hold=_hold, dispatch_id="disp-steer")
    result = await DispatchRunner(ctx).run(spec)

    assert result == ""  # detached — supervisor owns the task
    assert acks == [], "steer-only turn must not send a holding ack"
    assert flight.get("subagent_id"), "the detach itself happened"

    rows = await _subagent_rows(ctx)
    assert len(rows) == 1, "steer turn must still detach (frees the lock)"

    # wait for the supervisor to finish the run (steer origin on the run)
    run = None
    for _ in range(50):
        run = await ctx.db.fetch_one(
            "SELECT status, metadata_json FROM runs WHERE id = ?", (rows[0]["id"],))
        if run["status"] == "completed":
            break
        await asyncio.sleep(0.05)
    assert run["status"] == "completed"
    assert "steer_origin" in (run["metadata_json"] or "")

    msgs = await _messages(ctx)
    assert not [m for m in msgs if m["provenance"] in ("task_relay", "steer_relay")], (
        "a silent steer-born flight reports to nobody — uninvited speech "
        "was the v1 steer_relay apology shape")


async def test_steer_racing_human_message_keeps_the_ack(ctx, bb, stub_llm, stub_history):
    """A steer claimed in the same turn as a human message is not
    steer-only: the human half still deserves the holding ack."""
    from server.services.session_service import SessionService

    await SessionService(ctx).add_message(
        DM_KEY, "user", "what's happening outside?", channel="whatsapp", dispatched=0)
    await SessionService(ctx).add_message(
        DM_KEY, "user", "[Stimulus: frigate activity.person] person at doorbell",
        channel="whatsapp", dispatched=0, provenance="steer")
    acks: list[str] = []

    async def _hold(text: str) -> None:
        acks.append(text)

    spec = _spec(hold=_hold, dispatch_id="disp-mixed")
    await DispatchRunner(ctx).run(spec)

    assert acks, "human+steer turn keeps the holding ack"


def test_delivery_note_states_the_new_contract():
    """The detach-time note tells the flight the delivery contract for a
    detached turn (final-text delivery, 2026-10-01): final text is
    delivered by the supervisor, the send tool is for progress/media,
    NO_REPLY is silence. It must never suggest work — redo pressure here
    would reach a flight whose tool calls already executed for real."""
    from server.services.backburner import delivery_note
    note = delivery_note("abc12345", "send_whatsapp_message")
    assert note.startswith("[System note]")
    assert "bg turn abc12345" in note
    assert "delivered" in note and "automatically" in note
    assert "send_whatsapp_message" in note
    assert "NO_REPLY" in note
    # The old lie is gone — the note never says text is invisible:
    assert "NOT delivered" not in note
    assert "do not restate" in note.lower()


async def test_terminal_failed_flight_delivers_or_wakes(ctx, bb):
    """A failed flight delivers the failure directly when a send tool is
    available (with the honest real-effects header); the legacy wake only
    survives as the delivery-failure fallback (and boot recovery)."""
    sends: list[str] = []
    goal = await _settled_detached_task(
        ctx, flight={"subagent_id": "aaaabbbb", "sent": False, "texts": []},
        result_text="the upstream API 500'd twice", status="failed",
        sends=sends)

    assert sends, "failed flight delivers directly when it can"
    assert "failed" in sends[0]
    assert "may have had real effects" in sends[0]
    assert "500'd" in sends[0]
    msgs = await _messages(ctx)
    assert not [m for m in msgs if m["provenance"] == "task_relay"], (
        "direct delivery replaces the failed-flight wake")


async def test_terminal_failed_flight_never_delivers_raw_error_payloads(ctx, bb):
    """The 2026-10-02 AI-doom incident: an upstream 400 str()-ed to 56,471
    chars of raw provider Zod JSON and the failure notice delivered it
    wholesale into a group chat. Delivered failure text = the exception
    headline (one bounded line) + a pointer; the full error stays on the
    subagent row where it belongs."""
    zod = "[" + ", ".join(
        '{"code": "invalid_union", "errors": [["expected": "string", '
        '"received": "array"]]}' for _ in range(4000)) + "]"
    giant = (
        "Error code: 400 - {'error': {'code': 'invalid_prompt'}, "
        f"'metadata': {{'raw': '{zod}'}}}}")
    assert len(giant) > 50_000  # guard the guard: the incident class only
    sends: list[str] = []
    goal = await _settled_detached_task(
        ctx, flight={"subagent_id": "aaaabbbb", "sent": False, "texts": []},
        result_text=f"the background work failed: {giant}", status="failed",
        sends=sends)

    assert sends
    delivered = sends[0]
    assert len(delivered) < 600, (
        f"failure deliveries must be bounded, got {len(delivered)} chars")
    assert "invalid_prompt" in delivered          # the headline survives
    assert "invalid_union" not in delivered       # the payload never ships
    assert "full error on the task record" in delivered
    # the FULL error is still stored for forensics
    row = await ctx.db.fetch_one(
        "SELECT result FROM runs WHERE id = ?", ("aaaabbbb",))
    assert row and len(row["result"]) > 50_000
    assert "invalid_union" in row["result"]


async def test_terminal_completed_result_capped_for_delivery(ctx, bb):
    """A runaway 100k-char completion is delivered capped with a pointer;
    the goal keeps the full text."""
    sends: list[str] = []
    huge = "The frames were inspected. " * 4000
    assert len(huge) > 90_000
    goal = await _settled_detached_task(
        ctx, flight={"subagent_id": "aaaabbbb", "sent": False, "texts": [],
                     "tool_calls": 5},
        result_text=huge, sends=sends)

    assert sends
    assert len(sends[0]) < 5000
    assert "truncated" in sends[0]
    grow = await ctx.db.fetch_one(
        "SELECT result FROM runs WHERE id = ?", (goal["id"],))
    assert grow and len(grow["result"]) > 90_000


async def test_recovery_fails_running_flights_and_wakes_origin(ctx, bb):
    """Commitments Phase 0: a flight still running at boot died with the
    process — its run is failed and the origin conversation is woken
    (speak-expected) to own the loss. Steer-born flights stay quiet."""
    from server.repositories.runs import RunRepository

    repo = RunRepository(ctx.db)
    await repo.start(run_id="feedface", kind="flight", session_key=DM_KEY,
                     summary="checking bookings", now_iso="2026-10-05T00:00:00Z")
    await repo.start(run_id="0ddba11", kind="flight", session_key=DM_KEY,
                     summary="steer work", metadata={"steer_origin": True},
                     now_iso="2026-10-05T00:00:00Z")

    moved = await BackburnerService(ctx).recover_orphaned_goals()
    assert moved == 2

    for rid in ("feedface", "0ddba11"):
        assert (await repo.get(rid))["status"] == "failed"
    msgs = await _messages(ctx)
    lost = [m for m in msgs if "lost this background turn" in m["content"]]
    assert len(lost) == 1 and "checking bookings" in lost[0]["content"]
    assert lost[0]["provenance"] == "task_relay"


async def test_background_block_shows_running_flight(ctx, bb):
    """The goals block no longer carries flights as goals — a running
    flight renders in the background section instead, with its id."""
    from server.repositories.runs import RunRepository
    from server.services.context_assembler import ContextAssembler

    await RunRepository(ctx.db).start(
        run_id="cafe1234-0000", kind="flight", session_key=DM_KEY,
        summary="rendering the turntable", now_iso="2026-10-05T00:00:00Z")
    block = await ContextAssembler(ctx).goals_block(DM_KEY)
    assert "Running in the background" in block
    assert "bg turn cafe1234" in block and "rendering the turntable" in block
    assert "Active Goals" not in block


async def test_check_and_kill_resolve_flight_prefix(ctx, bb):
    from server.repositories.runs import RunRepository
    from server.services.subagent_service import SubagentService

    await RunRepository(ctx.db).start(
        run_id="beefcafe-1111", kind="flight", session_key=DM_KEY,
        summary="long job", now_iso="2026-10-05T00:00:00Z")
    svc = SubagentService(ctx)
    res = await svc.check_subagent("beefcafe", parent_session_key=DM_KEY)
    assert res["ok"] and res["status"] == "running"
    assert (await svc.check_subagent("beefcafe", parent_session_key="other"))["ok"] is False
    # no live task in the registry -> honest "nothing to cancel"
    res = await svc.kill_subagent("beefcafe", parent_session_key=DM_KEY)
    assert res["ok"] is False


@pytest.mark.asyncio
async def test_pre_detach_tool_calls_count_toward_honesty(ctx):
    """2026-10-06 AI doom: a turn killed one subagent and spawned another,
    THEN detached and only talked — the counter started at detach, so the
    group got an UNVERIFIED 'ran no tools' banner. Calls made before detach
    seed the counter; a truly tool-less turn still counts zero."""
    from server.services import backburner as bb
    await _seed_running_call(ctx, "d-pre", json.dumps([
        {"role": "user", "content": "write the bios"},
        {"type": "function_call", "name": "kill_subagent", "call_id": "c1", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        {"type": "function_call", "name": "create_subagent", "call_id": "c2", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c2", "output": "ok"},
    ]))
    assert await bb._pre_detach_tool_count(ctx, "d-pre") == 2
    await _seed_running_call(ctx, "d-none", json.dumps([
        {"role": "user", "content": "what band is playing tonight?"}]))
    assert await bb._pre_detach_tool_count(ctx, "d-none") == 0
    assert await bb._pre_detach_tool_count(ctx, None) == 0


async def test_terminal_spoken_flight_delivers_new_wrap_up(ctx, bb):
    """2026-10-06 card drop: the flight posted progress ("DMs going out one
    by one") and its final 'All done' wrap-up was dropped. New information
    after a progress send is delivered."""
    sends: list[str] = []
    await _settled_detached_task(
        ctx, flight={"subagent_id": "aaaabbbb", "sent": True, "tool_calls": 9,
                     "texts": ["Card drop starting — personal DMs going out one by one."]},
        result_text=("All done. Eight cards rendered from each person's reference "
                     "photo, montage posted, and all seven member cards delivered by DM."),
        sends=sends)
    assert sends and sends[0].startswith("All done.")


async def test_terminal_spoken_flight_paraphrase_stays_quiet(ctx, bb):
    """The duplicate-reply class: a final that restates what the flight
    already posted is not re-delivered."""
    sends: list[str] = []
    await _settled_detached_task(
        ctx, flight={"subagent_id": "aaaabbbb", "sent": True, "tool_calls": 4,
                     "texts": ["The Grand has rooms on Friday for $180 a night, booked one for you."]},
        result_text="Booked: The Grand has rooms Friday for $180 a night — one is booked for you.",
        sends=sends)
    assert sends == []
