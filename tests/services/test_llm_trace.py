"""llm_trace_events + reasoning capture (2026-10-03 trace uplift, Phase 1).

Covers: _TraceWriter row emission (kinds, caps, seq), _extract_reasoning's
two dialects (OpenAI summary + OpenRouter raw reasoning_text),
_request_reasoning's summary gating (small-cap floor, kill switch), the
dispatch run_turn wiring (rows land via on_round_complete, disabled
flag writes nothing), trace redaction, and the llm_call_log cascade.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.repositories.llm_trace import LlmTraceRepository
from server.services.llm_dispatch import LLMDispatchService, _TraceWriter
from server.services.openai_service import (
    _extract_reasoning, _request_reasoning)


# --------------------------------------------------------------- helpers

def _response(*output, text=""):
    return SimpleNamespace(output=list(output), output_text=text,
                           status="completed", refusal=None,
                           usage=SimpleNamespace(
        input_tokens=10, output_tokens=5, total_tokens=15,
        input_tokens_details=SimpleNamespace(cached_tokens=3)))


def _reasoning_item(summary=(), content=()):
    return SimpleNamespace(type="reasoning", id="rs_1",
                           summary=list(summary), content=list(content))


def _fc(name="get_weather", args='{"city": "Perth"}', call_id="call_1"):
    return SimpleNamespace(type="function_call", call_id=call_id,
                           name=name, arguments=args)


# ------------------------------------------------------- _extract_reasoning

@pytest.mark.asyncio
async def test_extract_reasoning_openai_summary_dialect():
    r = _response(_reasoning_item(
        summary=[SimpleNamespace(type="summary_text", text="Thinking it through")]))
    assert _extract_reasoning(r) == [
        {"source": "summary", "text": "Thinking it through"}]


@pytest.mark.asyncio
async def test_extract_reasoning_openrouter_raw_dialect():
    r = _response(_reasoning_item(
        content=[SimpleNamespace(type="reasoning_text", text="Classic bat and ball")]))
    assert _extract_reasoning(r) == [
        {"source": "raw", "text": "Classic bat and ball"}]


@pytest.mark.asyncio
async def test_extract_reasoning_both_and_order():
    r = _response(
        _reasoning_item(
            summary=[SimpleNamespace(type="summary_text", text="part one")],
            content=[SimpleNamespace(type="reasoning_text", text="part two")]),
        _fc())
    assert [p["text"] for p in _extract_reasoning(r)] == ["part one", "part two"]
    assert _extract_reasoning(_response(_fc())) == [], "no reasoning → no parts"


# ------------------------------------------------------ _request_reasoning

def _ls_settings(*, summary=True, floor=3000):
    return SimpleNamespace(
        config_dir=None,
        llm_streaming=SimpleNamespace(
            summary_enabled=summary, summary_min_output_tokens=floor))


@pytest.mark.asyncio
async def test_request_reasoning_adds_summary_above_floor():
    r = _request_reasoning("gpt-5.6-sol", "low", _ls_settings(), max_output_tokens=4000)
    assert r == {"effort": "low", "summary": "auto"}


@pytest.mark.asyncio
async def test_request_reasoning_summary_without_pinned_effort():
    """Flagship models with no models.yaml effort entry still get summaries —
    summary rides on its own at the model's default effort."""
    r = _request_reasoning("gpt-5.6-sol", None, _ls_settings())
    assert r == {"summary": "auto"}


@pytest.mark.asyncio
async def test_request_reasoning_skips_summary_below_floor():
    r = _request_reasoning("gpt-5.6-sol", "low", _ls_settings(), max_output_tokens=300)
    assert r == {"effort": "low"}, "capped passes keep their whole output budget"


@pytest.mark.asyncio
async def test_request_reasoning_kill_switch():
    r = _request_reasoning("gpt-5.6-sol", "low", _ls_settings(summary=False))
    assert r == {"effort": "low"}


@pytest.mark.asyncio
async def test_request_reasoning_non_reasoning_model():
    assert _request_reasoning("gpt-4.1-mini", None, _ls_settings()) is None


# ------------------------------------------------------------ _TraceWriter

@pytest.mark.asyncio
async def test_round_complete_writes_reasoning_tool_round_rows(ctx):
    await ctx.db.execute(
        "INSERT INTO llm_call_log (id, provider, call_category) VALUES "
        "('call-1', 'openai', 'chat')")
    writer = _TraceWriter(ctx.db, log_id="call-1", dispatch_id="d1",
                          session_key="sk", max_rows=400)
    await writer.round_complete(
        0,
        _response(
            _reasoning_item(summary=[SimpleNamespace(
                type="summary_text", text="check the weather tool")]),
            _fc()),
        1.234, None)
    await writer.close()

    events = await LlmTraceRepository(ctx.db).for_call("call-1")
    kinds = [e["kind"] for e in events]
    assert kinds == ["reasoning_part", "tool_call", "round_completed"]
    assert [e["seq"] for e in events] == [1, 2, 3], "call-local seq is monotonic"
    assert events[0]["content"] == "check the weather tool"
    assert events[0]["meta"] == {"source": "summary"}
    assert events[1]["meta"]["name"] == "get_weather"
    assert events[2]["meta"]["latency_seconds"] == 1.234
    assert events[2]["meta"]["tool_calls"] == 1


@pytest.mark.asyncio
async def test_writer_caps_content_and_rows(ctx):
    await ctx.db.execute(
        "INSERT INTO llm_call_log (id, provider, call_category) VALUES "
        "('call-2', 'openai', 'chat')")
    writer = _TraceWriter(ctx.db, log_id="call-2", dispatch_id=None,
                          session_key=None, max_rows=3)
    writer.emit("reasoning_part", content="x" * 5000)
    writer.emit("reasoning_part", content="y" * 5000)
    await writer.round_complete(0, _response(), 0.5, None)  # round_completed → row 3
    writer.emit("tool_result", content="late row past cap")
    await writer.close()

    events = await LlmTraceRepository(ctx.db).for_call("call-2")
    kinds = [e["kind"] for e in events]
    assert kinds == ["reasoning_part", "reasoning_part", "round_completed", "turn_note"]
    assert len(events[0]["content"]) <= 2000, "content capped at 2000 chars"
    assert "truncated" in events[-1]["content"]


@pytest.mark.asyncio
async def test_tool_results_group_with_their_round(ctx):
    """tool_result rows fire from the tool callback, which doesn't know the
    round — the writer tracks the last completed round's iteration so rows
    group correctly (regression: every result landed in round 0)."""
    await ctx.db.execute(
        "INSERT INTO llm_call_log (id, provider, call_category) VALUES "
        "('call-3', 'openai', 'chat')")
    writer = _TraceWriter(ctx.db, log_id="call-3", dispatch_id=None,
                          session_key=None, max_rows=50)
    await writer.round_complete(0, _response(_fc(call_id="c0")), 1.0, None)
    writer.emit("tool_result", content="round 0 result",
                meta={"name": "get_weather"})
    await writer.round_complete(1, _response(_fc(call_id="c1")), 2.0, None)
    writer.emit("tool_result", content="round 1 result",
                meta={"name": "bash"})
    await writer.close()

    events = await LlmTraceRepository(ctx.db).for_call("call-3")
    results = [(e["iteration"], e["content"]) for e in events
               if e["kind"] == "tool_result"]
    assert results == [(0, "round 0 result"), (1, "round 1 result")]


@pytest.mark.asyncio
async def test_writer_disabled_without_log_id(ctx):
    writer = _TraceWriter(ctx.db, log_id=None, dispatch_id=None,
                          session_key=None, max_rows=10)
    writer.emit("reasoning_part", content="never written")
    await writer.close()
    assert await LlmTraceRepository(ctx.db).for_call("nope") == []


# ------------------------------------------- dispatch wiring (run_turn)

@pytest.fixture
def fake_llm_service(monkeypatch, tmp_path):
    """OpenAIService with no network: first round calls a tool, second is
    text. Records the kwargs it was handed."""
    from server.services.openai_service import OpenAIService
    svc = object.__new__(OpenAIService)
    settings = SimpleNamespace(
        config_dir=tmp_path,  # no models.yaml → no effort/video lookups
        openai=SimpleNamespace(
            api_key="k", base_url="http://localhost:1", default_model="gpt-5.6-sol",
            memory_model="", web_search_enabled=False),
        openrouter=SimpleNamespace(enabled=False),
        self_wrap=SimpleNamespace(enabled=False, duration_fraction=0.75,
                                  iteration_margin=3),
    )
    svc._get_settings = lambda: settings
    calls: list[dict] = []

    class _Fake:
        def __init__(self, replies):
            self._replies = iter(replies)

        async def create(self, **kwargs):
            calls.append(kwargs)
            return next(self._replies)

    client = SimpleNamespace(responses=_Fake([
        _response(_fc("read_file", '{"path": "/tmp/x"}', "call_a")),
        _response(SimpleNamespace(
            type="message", role="assistant",
            content=[SimpleNamespace(type="output_text", text="done")]), text="done"),
    ]))
    svc._client_for = lambda model: client
    monkeypatch.setattr(LLMDispatchService, "_get_service", lambda self: svc)
    return calls


@pytest.mark.asyncio
async def test_dispatch_run_turn_writes_trace(ctx, fake_llm_service):
    from server.services.tools import Tool

    async def _handler(path: str) -> str:
        return "file contents"

    tools = [Tool(name="read_file", description="read", parameters={
        "type": "object", "properties": {"path": {"type": "string"}}},
        required=["path"], handler=_handler)]

    dispatch = LLMDispatchService(ctx)
    result = await dispatch.run_turn(
        [{"role": "user", "content": "read it"}], tools,
        call_category="test", session_key="sk-test", dispatch_id="disp-1")
    assert result == "done"

    log_id = (await ctx.db.fetch_one(
        "SELECT id FROM llm_call_log WHERE dispatch_id = 'disp-1'"))["id"]
    events = await LlmTraceRepository(ctx.db).for_call(log_id)
    kinds = [e["kind"] for e in events]
    assert kinds.count("tool_call") == 1
    assert "tool_result" in kinds, "tool callback writes result rows"
    assert kinds.count("round_completed") == 2, "one per round incl. final text"
    tool_call = next(e for e in events if e["kind"] == "tool_call")
    assert tool_call["meta"]["name"] == "read_file"


@pytest.mark.asyncio
async def test_dispatch_trace_kill_switch(ctx, fake_llm_service, monkeypatch):
    ctx.settings.llm_streaming.trace_enabled = False
    try:
        from server.services.tools import Tool

        async def _handler(path: str) -> str:
            return "x"

        tools = [Tool(name="read_file", description="read", parameters={
            "type": "object", "properties": {"path": {"type": "string"}}},
            required=["path"], handler=_handler)]
        dispatch = LLMDispatchService(ctx)
        await dispatch.run_turn(
            [{"role": "user", "content": "read it"}], tools,
            call_category="test", session_key="sk-test", dispatch_id="disp-2")
        n = await ctx.db.fetch_one("SELECT COUNT(*) AS n FROM llm_trace_events")
        assert n["n"] == 0, "BOB_LLM_TRACE=off writes nothing"
    finally:
        ctx.settings.llm_streaming.trace_enabled = True


# ------------------------------------------------- redaction + cascade

@pytest.mark.asyncio
async def test_trace_redaction_and_cascade(ctx):
    import server.heartbeat as heartbeat
    from datetime import datetime, timedelta, timezone

    await ctx.db.execute(
        """INSERT INTO llm_call_log (id, provider, call_category) VALUES
           ('old-call', 'openai', 'chat')""")
    writer = _TraceWriter(ctx.db, log_id="old-call", dispatch_id=None,
                          session_key=None, max_rows=10)
    writer.emit("reasoning_part", content="secret thinking", meta={"source": "raw"})
    await writer.close()

    old = (datetime.now(timezone.utc) - timedelta(days=45)).strftime(
        "%Y-%m-%d %H:%M:%S")
    await ctx.db.execute(
        "UPDATE llm_trace_events SET created_at = ?", (old,))

    heartbeat._last_llm_log_redaction = None
    await heartbeat.LlmLogRetentionTask().run(ctx)

    row = await ctx.db.fetch_one(
        "SELECT content, meta_json FROM llm_trace_events WHERE llm_call_id = 'old-call'")
    assert row["content"] == ""
    assert row["meta_json"] is None

    # cascade: deleting the llm_call_log row takes its trace rows
    await ctx.db.execute("DELETE FROM llm_call_log WHERE id = 'old-call'")
    n = await ctx.db.fetch_one(
        "SELECT COUNT(*) AS n FROM llm_trace_events WHERE llm_call_id = 'old-call'")
    assert n["n"] == 0


# -------------------------------------- backburner transcript ignores reasoning

@pytest.mark.asyncio
async def test_backburner_transcript_ignores_reasoning_items():
    import json as _json
    from server.services.backburner import build_transcript
    items = [
        {"role": "user", "content": "whats the weather"},
        {"type": "reasoning", "id": "rs_1",
         "summary": [{"type": "summary_text", "text": "long thinking here"}]},
        {"type": "function_call", "call_id": "call_1", "name": "get_weather",
         "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_1", "output": "sunny"},
    ]
    text = build_transcript(_json.dumps(items), max_tail=10)
    assert "long thinking here" not in text, "reasoning must not reach the probe prompt"
    assert "calls get_weather" in text
