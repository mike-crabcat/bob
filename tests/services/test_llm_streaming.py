"""Streaming transport in run_turn (2026-10-03 trace uplift, Phase 2).

The contract: with BOB_LLM_STREAMING on, each tool-loop round rides a
streamed ``responses.create``; deltas are forwarded via on_stream_event
(both reasoning dialects — OpenAI summary parts and OpenRouter/GLM raw
reasoning_text); the round returns ``event.response`` from
response.completed/incomplete so tool execution, citation rendering, and
trace extraction are transport-agnostic. With the flag off (default), the
request wire is byte-identical to the pre-uplift buffered call.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.services.openai_service import OpenAIService, StreamResult


def _settings(tmp_path, *, streaming: bool, wrap_enabled: bool = True):
    return SimpleNamespace(
        config_dir=tmp_path,  # real path: supports_video() reads models.yaml
        openai=SimpleNamespace(
            api_key="k", base_url="http://localhost:1", default_model="gpt-5.6-sol",
            memory_model="", web_search_enabled=False),
        openrouter=SimpleNamespace(enabled=False),
        self_wrap=SimpleNamespace(enabled=wrap_enabled, duration_fraction=0.75,
                                  iteration_margin=3),
        llm_streaming=SimpleNamespace(
            streaming_enabled=streaming, summary_enabled=False,
            summary_min_output_tokens=3000, delta_min_interval_ms=250,
            trace_enabled=True, trace_max_rows_per_call=400),
    )


def _completed_response(output, *, text="", usage=None):
    return SimpleNamespace(output=list(output), output_text=text,
                           status="completed", refusal=None, usage=usage)


def _fc(name="get_weather", args='{"city": "Perth"}', call_id="call_1"):
    return SimpleNamespace(type="function_call", call_id=call_id,
                           name=name, arguments=args)


def _msg(text):
    return SimpleNamespace(type="message", role="assistant",
                           content=[SimpleNamespace(type="output_text", text=text)])


def _usage():
    return SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15,
                           input_tokens_details=SimpleNamespace(cached_tokens=0))


class _FakeStream:
    """Async iterator of typed events (the Responses streaming shape)."""

    def __init__(self, events):
        self._events = events

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


def _ev(etype, **fields):
    return SimpleNamespace(type=etype, **fields)


def _service(monkeypatch, tmp_path, *, streaming: bool, streams):
    """streams: list of event lists, one per round (consumed in order)."""
    svc = object.__new__(OpenAIService)
    svc._get_settings = lambda: _settings(tmp_path, streaming=streaming)
    calls: list[dict] = []
    queue = list(streams)

    class _Fake:
        async def create(self, **kwargs):
            calls.append(kwargs)
            if kwargs.get("stream"):
                return _FakeStream(queue.pop(0) if queue else [])
            return queue.pop(0)[0].response  # buffered: last word = response

    client = SimpleNamespace(responses=_Fake())
    svc._client_for = lambda model: client
    return svc, calls


# ------------------------------------------------------------- streamed loop

@pytest.mark.asyncio
async def test_streamed_rounds_forward_deltas_and_drive_tools(monkeypatch, tmp_path):
    tool_calls: list[dict] = []

    async def handler(city: str) -> str:
        tool_calls.append({"city": city})
        return "sunny, 24"

    svc, calls = _service(monkeypatch, tmp_path, streaming=True, streams=[
        # round 1: OpenAI summary dialect + function call
        [_ev("response.reasoning_summary_text.delta", delta="check "),
         _ev("response.reasoning_summary_text.delta", delta="the tool"),
         _ev("response.reasoning_summary_part.done",
             part=SimpleNamespace(text="check the tool")),
         _ev("response.output_item.added",
             item=SimpleNamespace(type="function_call", name="get_weather",
                                  id="fc_1", call_id="call_1")),
         _ev("response.function_call_arguments.delta",
             item_id="fc_1", delta='{"city": '),
         _ev("response.function_call_arguments.delta",
             item_id="fc_1", delta='"Perth"}'),
         _ev("response.completed",
             response=_completed_response([_fc()], usage=_usage()))],
        # round 2: raw GLM dialect + final text
        [_ev("response.reasoning_text.delta", delta="the answer"),
         _ev("response.reasoning_text.done", text="the answer"),
         _ev("response.output_text.delta", delta="It is sun"),
         _ev("response.output_text.delta", delta="ny today."),
         _ev("response.completed",
             response=_completed_response([_msg("It is sunny today.")],
                                          text="It is sunny today.",
                                          usage=_usage()))],
    ])

    seen: list[tuple[str, dict]] = []

    async def on_stream_event(kind, data):
        seen.append((kind, dict(data)))

    stream_result = StreamResult()
    result = await svc.run_turn(
        [{"role": "user", "content": "weather?"}],
        tools=[], tool_handlers={"get_weather": handler},
        stream_result=stream_result,
        on_stream_event=on_stream_event,
    )

    assert result == "It is sunny today."
    assert tool_calls == [{"city": "Perth"}], "tool execution driven by event.response.output"
    assert calls[0].get("stream") is True and calls[1].get("stream") is True

    kinds = [k for k, _ in seen]
    # round 1: summary dialect deltas → whole part → tool start → arg deltas
    assert kinds[:7] == ["round_started", "reasoning_delta", "reasoning_delta",
                         "reasoning_part", "tool_started", "tool_args", "tool_args"]
    assert seen[3][1]["text"] == "check the tool"
    assert seen[3][1]["raw"] is False
    # round 2: raw GLM dialect + text deltas
    assert kinds[7:] == ["round_started", "reasoning_delta", "reasoning_part",
                         "text_delta", "text_delta"]
    assert seen[9][1]["raw"] is True
    assert stream_result.ttft_seconds is not None, "TTFT stamped on first delta"
    assert stream_result.total_tokens == 30, "usage from response.completed"


@pytest.mark.asyncio
async def test_incomplete_terminal_returns_response(monkeypatch, tmp_path):
    svc, calls = _service(monkeypatch, tmp_path, streaming=True, streams=[
        [_ev("response.output_text.delta", delta="partial"),
         _ev("response.incomplete",
             response=_completed_response([_msg("partial")], text="partial"))],
    ])
    result = await svc.run_turn(
        [{"role": "user", "content": "hi"}], tools=[], tool_handlers={})
    assert result == "partial"


@pytest.mark.asyncio
async def test_reasoning_survives_empty_completed_item(monkeypatch, tmp_path):
    """Some OpenRouter hosts stream reasoning_text events but return the
    completed response's reasoning item with content null (live 2026-10-03:
    routine turn streamed 2587 chars, trace got 0 rows). The stream is ground
    truth: _round accumulates the done-events and _extract_reasoning falls
    back to them — the round's reasoning must survive into the trace feed."""
    from server.services.openai_service import _extract_reasoning

    rounds: list[Any] = []

    async def on_round_complete(iteration, response, latency, usage):
        rounds.append(_extract_reasoning(response))

    svc, _ = _service(monkeypatch, tmp_path, streaming=True, streams=[
        [_ev("response.reasoning_text.delta", delta="Think "),
         _ev("response.reasoning_text.delta", delta="about the weather"),
         _ev("response.reasoning_text.done", text="Think about the weather"),
         _ev("response.completed",
             # completed item carries NO summary/content — host quirk shape
             response=_completed_response([
                 SimpleNamespace(type="reasoning", id="rs_1", summary=[],
                                 content=None, encrypted_content=None),
                 _msg("sunny")], text="sunny", usage=_usage()))],
    ])
    result = await svc.run_turn(
        [{"role": "user", "content": "weather?"}], tools=[], tool_handlers={},
        on_round_complete=on_round_complete)
    assert result  # ran to completion shape
    assert rounds and rounds[0] == [
        {"source": "raw", "text": "Think about the weather"}], (
        "stream-captured reasoning must reach on_round_complete even when "
        "the completed item is empty")


@pytest.mark.asyncio
async def test_reasoning_prefers_completed_item_no_double_count(monkeypatch, tmp_path):
    """When the completed response DOES carry the reasoning (most hosts,
    OpenAI-direct), the stream fallback must not duplicate it."""
    from server.services.openai_service import _extract_reasoning

    rounds: list[Any] = []

    async def on_round_complete(iteration, response, latency, usage):
        rounds.append(_extract_reasoning(response))

    svc, _ = _service(monkeypatch, tmp_path, streaming=True, streams=[
        [_ev("response.reasoning_summary_text.delta", delta="summary text"),
         _ev("response.completed",
             response=_completed_response([
                 SimpleNamespace(type="reasoning", id="rs_1",
                                 summary=[SimpleNamespace(
                                     type="summary_text", text="summary text")],
                                 content=[], encrypted_content=None)]))],
    ])
    await svc.run_turn(
        [{"role": "user", "content": "hi"}], tools=[], tool_handlers={},
        on_round_complete=on_round_complete)
    assert rounds[0] == [{"source": "summary", "text": "summary text"}]


@pytest.mark.asyncio
async def test_failed_event_raises(monkeypatch, tmp_path):
    svc, _ = _service(monkeypatch, tmp_path, streaming=True, streams=[
        [_ev("response.failed",
             response=SimpleNamespace(error=SimpleNamespace(message="boom")))],
    ])
    with pytest.raises(RuntimeError, match="stream failed"):
        await svc.run_turn(
            [{"role": "user", "content": "hi"}], tools=[], tool_handlers={})


@pytest.mark.asyncio
async def test_stream_without_terminal_raises(monkeypatch, tmp_path):
    svc, _ = _service(monkeypatch, tmp_path, streaming=True, streams=[
        [_ev("response.output_text.delta", delta="orphan")],
    ])
    with pytest.raises(RuntimeError, match="without a terminal event"):
        await svc.run_turn(
            [{"role": "user", "content": "hi"}], tools=[], tool_handlers={})


# --------------------------------------------------------- buffered default

@pytest.mark.asyncio
async def test_flag_off_is_byte_identical_buffered(monkeypatch, tmp_path):
    """Kill switch (and the shipped default): no stream kwarg, no forwarded
    deltas, same result — the pre-uplift wire contract."""
    response = _completed_response([_msg("buffered reply")], text="buffered reply")
    svc, calls = _service(monkeypatch, tmp_path, streaming=False, streams=[
        [_ev("response.completed", response=response)]])

    seen: list[str] = []

    async def on_stream_event(kind, data):
        seen.append(kind)

    result = await svc.run_turn(
        [{"role": "user", "content": "hi"}], tools=[], tool_handlers={},
        on_stream_event=on_stream_event)
    assert result == "buffered reply"
    assert "stream" not in calls[0], "buffered requests must not ask to stream"
    assert seen == ["round_started"], "only round boundaries fire, never deltas"


# ------------------------------------------------------------- wrap-up round

@pytest.mark.asyncio
async def test_forced_wrapup_round_streams(monkeypatch, tmp_path):
    svc, calls = _service(monkeypatch, tmp_path, streaming=True, streams=[
        # the wrap-up round: budget already spent at loop entry
        [_ev("response.output_text.delta", delta="wrapping "),
         _ev("response.output_text.delta", delta="up"),
         _ev("response.completed",
             response=_completed_response([_msg("wrapping up")],
                                          text="wrapping up"))],
    ])
    seen: list[tuple[str, dict]] = []

    async def on_stream_event(kind, data):
        seen.append((kind, dict(data)))

    result = await svc.run_turn(
        [{"role": "user", "content": "bulletin"}],
        tools=[], tool_handlers={},
        time_limit_seconds=0.0,
        on_stream_event=on_stream_event,
    )
    assert result == "wrapping up"
    kinds = [k for k, _ in seen]
    assert kinds == ["round_started", "text_delta", "text_delta"]
    assert seen[0][1]["iteration"] == -1, "wrap-up round is marked as such"
