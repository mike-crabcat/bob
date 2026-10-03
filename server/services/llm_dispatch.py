"""Unified LLM dispatch service — logs all LLM interactions."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4


def _content_char_len(content: Any) -> int:
    """Return character length of message content, handling both str and list[dict]."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(part.get("text", "")) if isinstance(part, dict) else 0
            for part in content
        )
    return 0

from server.services.tools import Tool

from server.services import model_registry
from server.services import quota_gate
from server.services.base import BaseService
from server.services.openai_service import (
    OpenAIService, StreamResult, _effective_reasoning_effort, _extract_reasoning)

logger = logging.getLogger(__name__)


# Tracks whether memory-read tools were used during a given dispatch, so that
# the resulting assistant message can be flagged as synthetic (an echo of
# existing memory rather than new ground truth). Keyed on dispatch_id.
_memory_tool_used: dict[str, bool] = {}
_MEMORY_TOOL_NAMES = frozenset({"recall", "find"})

# Per-dispatch tool-call trace, populated after chat_with_tools completes and
# consumed by SessionService.add_message via pop_tool_trace(). Mirrors the
# _memory_tool_used pattern. Each value is {"items": [...], "summary": str}.
_dispatch_tool_trace: dict[str, dict[str, Any]] = {}

# Item types from the Responses API output that we persist for replay.
# Reasoning items are dropped here deliberately — this trace replays onto
# FUTURE request wires, where stale reasoning items don't belong. Reasoning
# TEXT is captured per-round in llm_trace_events instead (2026-10-03 uplift).
_PERSISTED_ITEM_TYPES = frozenset({"function_call", "function_call_output", "message"})

# Per-string cap on function_call.arguments and function_call_output.output,
# and whole-row cap on the serialized items JSON. Oversized rows fall back to
# summary-only.
_ITEM_CAP = 8192
_WHOLE_TRACE_CAP = 65536


def _truncate_str(s: Any, limit: int) -> str:
    if not isinstance(s, str):
        try:
            s = json.dumps(s, default=str)
        except Exception:
            s = str(s)
    return s if len(s) <= limit else s[:limit] + "…[truncated]"


def _is_image_user_block(item: dict[str, Any]) -> bool:
    """Detect the synthetic {role: user, content: [input_image/input_video, ...]}
    block that OpenAIService appends after an ImageInjection/VideoInjection
    tool result."""
    if item.get("role") != "user":
        return False
    content = item.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(p, dict) and p.get("type") in ("input_image", "input_video")
        for p in content
    )


def _cap_item(item: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of item with oversized string fields truncated."""
    t = item.get("type")
    if t == "function_call_output":
        out = item.get("output")
        if isinstance(out, str) and len(out) > _ITEM_CAP:
            return {**item, "output": out[:_ITEM_CAP] + "…[truncated]"}
    elif t == "function_call":
        args = item.get("arguments")
        if isinstance(args, str) and len(args) > _ITEM_CAP:
            return {**item, "arguments": args[:_ITEM_CAP] + "…[truncated]"}
    return item


def _summarize_call(fc: dict[str, Any], fco: dict[str, Any]) -> str:
    name = fc.get("name", "?")
    args_raw = fc.get("arguments", "{}")
    try:
        args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
    except (json.JSONDecodeError, TypeError):
        args = {}
    arg_parts = [f"{k}={_truncate_str(v, 80)}" for k, v in args.items()]
    args_preview = ", ".join(arg_parts)
    out_preview = _truncate_str(fco.get("output", ""), 80)
    return f"{name}({args_preview}) → {out_preview}"


def _build_tool_trace(new_items: list[Any]) -> dict[str, Any] | None:
    """Filter, cap, and summarize the items a dispatch appended to messages.

    Returns None when the dispatch made no function_call items (i.e. it was a
    pure text reply with no tool work worth replaying).
    """
    filtered: list[dict[str, Any]] = []
    for item in new_items:
        if not isinstance(item, dict):
            continue
        if _is_image_user_block(item):
            continue
        if item.get("type") in _PERSISTED_ITEM_TYPES:
            filtered.append(_cap_item(item))

    if not any(it.get("type") == "function_call" for it in filtered):
        return None

    pending_fc: dict[str, dict[str, Any]] = {}
    summary_parts: list[str] = []
    for item in filtered:
        t = item.get("type")
        if t == "function_call":
            call_id = item.get("call_id")
            if call_id:
                pending_fc[call_id] = item
        elif t == "function_call_output":
            call_id = item.get("call_id")
            fc = pending_fc.pop(call_id, None) if call_id else None
            if fc:
                summary_parts.append(_summarize_call(fc, item))

    summary = "[tools used: " + "; ".join(summary_parts) + "]" if summary_parts else ""
    return {"items": filtered, "summary": summary}


def _serialize_trace_items(trace: dict[str, Any] | None) -> str | None:
    """Serialize a trace's items list to JSON, capped at _WHOLE_TRACE_CAP.

    Returns None when trace is None, on serialization failure, or when the
    serialized form exceeds the cap (caller falls back to summary-only).
    """
    if trace is None:
        return None
    try:
        items_json = json.dumps(_sanitize_for_json(trace.get("items", [])))
    except Exception:
        return None
    if len(items_json) > _WHOLE_TRACE_CAP:
        return None
    return items_json


def _sanitize_for_json(obj: Any) -> Any:
    """Recursively convert non-serializable objects to plain dicts."""
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    if hasattr(obj, "model_dump"):
        return _sanitize_for_json(obj.model_dump())
    if hasattr(obj, "__dict__"):
        return _sanitize_for_json(vars(obj))
    return str(obj)


# Reasoning/args/result text cap per trace row — enough to mine behaviour,
# small enough that one verbose round can't bloat the table.
_TRACE_CONTENT_CAP = 2000


class _TraceWriter:
    """Per-call llm_trace_events writer (2026-10-03 trace uplift).

    Coalesces a round's rows in memory and flushes once per round, so a turn
    with N rounds costs N batch writes. Rows carry a call-local ``seq`` so the
    dashboard timeline and the live llm.stream.* events order identically.
    Content rows stop at ``max_rows`` (pathological-turn bound); a turn_note
    marks the truncation. All failures are swallowed — tracing must never
    kill a turn.
    """

    def __init__(
        self, db: Any, *, log_id: str | None, dispatch_id: str | None,
        session_key: str | None, max_rows: int,
    ) -> None:
        self.db = db
        self.log_id = log_id
        self.dispatch_id = dispatch_id
        self.session_key = session_key
        self.max_rows = max_rows
        self.seq = 0
        self.written = 0
        self.truncated = False
        # Iteration whose tools are currently executing — set by
        # round_complete so tool_result rows group with their round.
        self.iteration = 0
        self._pending: list[dict[str, Any]] = []

    @property
    def enabled(self) -> bool:
        return self.log_id is not None

    def emit(
        self, kind: str, *, iteration: int | None = None, content: str = "",
        meta: dict[str, Any] | None = None,
    ) -> None:
        if not self.enabled or self.truncated:
            return
        if self.written + len(self._pending) >= self.max_rows:
            self.truncated = True
            return
        self.seq += 1
        self._pending.append({
            "dispatch_id": self.dispatch_id,
            "session_key": self.session_key,
            "iteration": self.iteration if iteration is None else iteration,
            "seq": self.seq,
            "kind": kind,
            "content": content[:_TRACE_CONTENT_CAP],
            "meta": meta,
        })

    async def flush(self) -> None:
        if not self._pending or not self.enabled:
            return
        from server.repositories.llm_trace import LlmTraceRepository
        try:
            count = await LlmTraceRepository(self.db).append_many(
                llm_call_id=self.log_id, events=self._pending)
            self.written += count
            self._pending.clear()
        except Exception:
            logger.warning(
                "trace flush failed: log_id=%s dispatch_id=%s rows=%d",
                self.log_id, self.dispatch_id, len(self._pending), exc_info=True)
            self._pending.clear()

    async def close(self) -> None:
        """Final flush + truncation marker. Call at every exit path."""
        if self.truncated:
            # Bypasses emit() deliberately — the marker must land even though
            # the cap is what it's marking.
            self.seq += 1
            self._pending.append({
                "dispatch_id": self.dispatch_id,
                "session_key": self.session_key,
                "iteration": 0,
                "seq": self.seq,
                "kind": "turn_note",
                "content": f"trace truncated at {self.max_rows} rows",
                "meta": {"max_rows": self.max_rows},
            })
            self.truncated = False
        await self.flush()

    async def round_complete(
        self, iteration: int, response: Any, round_latency: float, usage: Any,
    ) -> None:
        """on_round_complete adapter: reasoning parts + tool_call rows +
        round_completed meta, flushed as one batch. Transport-agnostic —
        buffered and streamed rounds feed identical rows. Also stamps the
        writer's iteration so the tool_result rows that follow (this round's
        executions) group correctly."""
        if not self.enabled:
            return
        self.iteration = iteration
        for part in _extract_reasoning(response):
            self.emit(
                "reasoning_part", iteration=iteration,
                content=part["text"], meta={"source": part["source"]})
        tool_calls = [
            item for item in (getattr(response, "output", None) or [])
            if getattr(item, "type", None) == "function_call"]
        for fc in tool_calls:
            self.emit(
                "tool_call", iteration=iteration,
                content=json.dumps(
                    {"name": fc.name, "arguments": fc.arguments},
                    default=str),
                meta={"call_id": fc.call_id, "name": fc.name})
        self.emit(
            "round_completed", iteration=iteration,
            content="",
            meta={
                "latency_seconds": round(round_latency, 3),
                "tool_calls": len(tool_calls),
                "prompt_tokens": getattr(usage, "input_tokens", None) if usage else None,
                "completion_tokens": getattr(usage, "output_tokens", None) if usage else None,
                "cached_tokens": None if not usage else (
                    getattr(getattr(usage, "input_tokens_details", None),
                            "cached_tokens", None)),
            })
        await self.flush()


def _extract_from_messages(
    messages: list[dict[str, Any]],
) -> tuple[str, str]:
    """Extract system_prompt and user_message from a messages array."""
    system_prompt = ""
    user_message = ""
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            system_prompt = content if isinstance(content, str) else ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            user_message = content if isinstance(content, str) else ""
            break
    return system_prompt, user_message


async def _record_log(
    db: Any,
    *,
    log_id: str | None = None,
    provider: str = "",
    model: str = "",
    call_category: str = "",
    session_key: str | None = None,
    system_prompt: str = "",
    user_message: str = "",
    messages_json: str | None = None,
    tools_json: str | None = None,
    response_text: str = "",
    latency_seconds: float | None = None,
    ttft_seconds: float | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    cached_tokens: int | None = None,
    status: str = "completed",
    error_message: str | None = None,
    project_id: str | None = None,
    task_id: str | None = None,
    dispatch_id: str | None = None,
    contact_id: str | None = None,
    tool_blocks_json: str | None = None,
    generation_id: str | None = None,
    reasoning_effort: str | None = None,
) -> str:
    """Record or update an LLM call log entry. Returns the log_id.

    If log_id is provided and a row with that id exists, UPDATE it.
    Otherwise INSERT a new row.
    """
    from server.repositories.llm_call_log import LlmCallLogRepository
    try:
        return await LlmCallLogRepository(db).upsert(
            log_id=log_id, provider=provider, model=model,
            call_category=call_category, session_key=session_key,
            system_prompt=system_prompt, user_message=user_message,
            messages_json=messages_json, tools_json=tools_json,
            response_text=response_text, latency_seconds=latency_seconds,
            ttft_seconds=ttft_seconds, prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens, total_tokens=total_tokens,
            cached_tokens=cached_tokens, status=status,
            error_message=error_message, project_id=project_id,
            task_id=task_id, dispatch_id=dispatch_id, contact_id=contact_id,
            tool_blocks_json=tool_blocks_json,
            generation_id=generation_id,
            reasoning_effort=reasoning_effort)
    except Exception:
        logger.warning("Failed to record LLM call log", exc_info=True)
        return log_id or str(uuid4())


class LLMDispatchService(BaseService):
    """Routes LLM calls to OpenAI and logs all interactions."""

    async def _publish_call(self, *, status: str, session_key: str | None,
                            call_category: str, model: str, latency_seconds: float | None,
                            total_tokens: int | None, **kwargs: Any) -> None:
        if self.ctx.event_bus is None:
            return
        event_type = f"llm.call.{status}"
        await self.ctx.event_bus.publish(event_type, {
            "session_key": session_key,
            "call_category": call_category,
            "model": model,
            "status": status,
            "latency_seconds": latency_seconds,
            "total_tokens": total_tokens,
            **kwargs,
        })

    def _get_service(self) -> OpenAIService:
        return OpenAIService(self.ctx)

    def _make_stream_callback(
        self, *, session_key: str | None, call_category: str, model: str,
        log_id: str | None, dispatch_id: str | None,
    ) -> Any:
        """Map OpenAIService stream kinds → live ``llm.stream.*`` bus events.

        Deliberately a NEW prefix — ``__root.tsx`` invalidates three queries
        on every ``llm.call.*`` event, so per-delta traffic must never ride
        that prefix. Deltas (text/reasoning/tool args) are batched per
        delta_min_interval_ms; whole reasoning parts and round boundaries
        publish immediately. Every payload carries ``seq`` (bus-local,
        monotonic) so the UI can detect drop-oldest gaps and refetch the
        trace endpoint. Deltas are ephemeral — durability lives in
        llm_trace_events via on_round_complete."""
        ls = getattr(self._get_settings(), "llm_streaming", None)
        interval = ((getattr(ls, "delta_min_interval_ms", 250) or 0) / 1000.0
                    if ls is not None else 0.25)
        state: dict[str, Any] = {"seq": 0, "last_flush": 0.0, "buf": {}}

        async def _publish(kind: str, payload: dict[str, Any]) -> None:
            if self.ctx.event_bus is None:
                return
            state["seq"] += 1
            base: dict[str, Any] = {"seq": state["seq"]}
            if log_id:
                base["log_id"] = log_id
            if dispatch_id:
                base["dispatch_id"] = dispatch_id
            if session_key:
                base["session_key"] = session_key
            base["call_category"] = call_category
            base["model"] = model
            await self.ctx.event_bus.publish(f"llm.stream.{kind}", {**base, **payload})

        async def _flush(force: bool) -> None:
            if not state["buf"]:
                return
            now = time.monotonic()
            if not force and now - state["last_flush"] < interval:
                return
            for kind, payload in state["buf"].items():
                await _publish(kind, payload)
            state["buf"].clear()
            state["last_flush"] = now

        async def _on_stream_event(kind: str, data: dict[str, Any]) -> None:
            if kind in ("text_delta", "reasoning_delta", "tool_args"):
                if kind == "tool_args":
                    item_id = data.get("item_id") or "?"
                    slot = state["buf"].setdefault(
                        "tool", {"args": "", "item_id": item_id,
                                 "phase": "args", "iteration": data.get("iteration")})
                    slot["args"] += data.get("delta") or ""
                else:
                    slot = state["buf"].setdefault(
                        "text" if kind == "text_delta" else "reasoning",
                        {"text": "", "iteration": data.get("iteration")})
                    slot["text"] += data.get("text") or ""
                    if data.get("raw"):
                        slot["raw"] = True
                await _flush(force=False)
            elif kind == "reasoning_part":
                await _flush(force=True)  # deltas before the part that closes them
                await _publish("reasoning", {
                    "text": data.get("text") or "", "done": True,
                    "raw": bool(data.get("raw")),
                    "iteration": data.get("iteration")})
            elif kind == "tool_started":
                await _flush(force=True)
                await _publish("tool", {
                    "name": data.get("name"), "item_id": data.get("item_id"),
                    "phase": "started", "iteration": data.get("iteration")})
            elif kind == "round_started":
                await _flush(force=True)
                await _publish("round", {
                    "phase": "started", "iteration": data.get("iteration")})
            elif kind == "round_finished":
                # internal: fired by the dispatch round handler so the
                # completed tick shares this callback's seq counter
                await _flush(force=True)
                await _publish("round", {
                    "phase": "completed", "iteration": data.get("iteration"),
                    "latency_seconds": data.get("latency_seconds"),
                    "tool_calls": data.get("tool_calls")})
        return _on_stream_event

    def _make_tool_callback(
        self,
        session_key: str | None,
        call_category: str,
        log_id: str | None = None,
        dispatch_id: str | None = None,
        trace: _TraceWriter | None = None,
    ) -> Any:
        async def _on_tool_call(name: str, args: dict, result_summary: str) -> None:
            if dispatch_id:
                if name in _MEMORY_TOOL_NAMES:
                    _memory_tool_used[dispatch_id] = True
                # Detached-flight honesty ledger (backburner, 2026-09-17):
                # zero-call silent flights narrate work that never happened;
                # count every executed call so the relay can tell.
                try:
                    from server.services import backburner as _bb
                    _bb.note_tool_call(dispatch_id)
                except Exception:
                    pass
            if trace is not None:
                trace.emit(
                    "tool_result",
                    iteration=trace.iteration,
                    content=_truncate_str(result_summary, _TRACE_CONTENT_CAP),
                    meta={"name": name, "args": _truncate_str(args, 400)})
            if self.ctx.event_bus is None:
                return
            payload: dict[str, Any] = {
                "session_key": session_key,
                "call_category": call_category,
                "tool_name": name,
                "tool_args": args,
                "tool_output": result_summary,
            }
            if log_id:
                payload["log_id"] = log_id
            if dispatch_id:
                payload["dispatch_id"] = dispatch_id
            await self.ctx.event_bus.publish("llm.call.tool_completed", payload)
        return _on_tool_call

    @staticmethod
    def pop_memory_used(dispatch_id: str | None) -> bool:
        """Return and clear the memory-used flag for a dispatch.

        Returns False when dispatch_id is None or no memory-read tool fired.
        """
        if not dispatch_id:
            return False
        return _memory_tool_used.pop(dispatch_id, False)

    @staticmethod
    def pop_tool_trace(dispatch_id: str | None) -> dict[str, Any] | None:
        """Return and clear the tool trace for a dispatch.

        Returns None when dispatch_id is None or no trace was captured.
        Otherwise returns ``{"summary": str, "items_json": str | None}``.
        ``items_json`` is None when the serialized items exceeded
        ``_WHOLE_TRACE_CAP`` — caller falls back to summary-only.
        """
        if not dispatch_id:
            return None
        trace = _dispatch_tool_trace.pop(dispatch_id, None)
        if trace is None:
            return None
        return {
            "summary": trace.get("summary", ""),
            "items_json": _serialize_trace_items(trace),
        }

    def _resolve_model(self, model: str | None = None) -> str:
        if model:
            return model
        return self._get_settings().openai.default_model

    @property
    def memory_model(self) -> str:
        return self._get_settings().openai.get_memory_model()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        call_category: str = "quick_prompt",
        session_key: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        dispatch_id: str | None = None,
        contact_id: str | None = None,
    ) -> str:
        """Non-streaming chat completion with automatic logging."""
        resolved_model = self._resolve_model(model)
        provider = model_registry.provider_for(resolved_model)
        quota_gate.check(provider)
        service = self._get_service()

        system_prompt, user_message = _extract_from_messages(messages)
        messages_json = json.dumps(messages)
        t0 = time.monotonic()

        try:
            stream_result = StreamResult()
            call_meta: dict[str, Any] = {}
            result = await service.chat(
                messages=messages,
                model=resolved_model,
                temperature=temperature,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                stream_result=stream_result,
                call_meta=call_meta,
            )
            elapsed = time.monotonic() - t0

            log_id = await _record_log(
                self.db,
                provider=provider,
                model=resolved_model,
                call_category=call_category,
                session_key=session_key,
                system_prompt=system_prompt,
                user_message=user_message,
                messages_json=messages_json,
                generation_id=call_meta.get("generation_id"),
                reasoning_effort=call_meta.get("reasoning_effort"),
                response_text=result or "",
                latency_seconds=elapsed,
                prompt_tokens=stream_result.prompt_tokens,
                completion_tokens=stream_result.completion_tokens,
                total_tokens=stream_result.total_tokens,
                cached_tokens=stream_result.cached_tokens,
                status="completed",
                project_id=project_id,
                task_id=task_id,
                dispatch_id=dispatch_id,
                contact_id=contact_id,
            )
            # Single-round trace (2026-10-03): background passes (memory,
            # dream, reflection) don't run tool loops, but their reasoning is
            # just as mineable — one round's rows at completion.
            ls = getattr(self._get_settings(), "llm_streaming", None)
            if ls is None or getattr(ls, "trace_enabled", True):
                trace = _TraceWriter(
                    self.db, log_id=log_id, dispatch_id=dispatch_id,
                    session_key=session_key,
                    max_rows=getattr(ls, "trace_max_rows_per_call", 400)
                    if ls is not None else 400)
                for part in call_meta.get("reasoning_parts") or []:
                    trace.emit("reasoning_part", content=part["text"],
                               meta={"source": part["source"]})
                trace.emit(
                    "round_completed",
                    meta={"latency_seconds": round(elapsed, 3), "tool_calls": 0,
                          "prompt_tokens": stream_result.prompt_tokens,
                          "completion_tokens": stream_result.completion_tokens,
                          "cached_tokens": stream_result.cached_tokens})
                await trace.close()

            quota_gate.record_success(provider)
            logger.info(
                "LLM dispatch: model=%s category=%s latency=%.2fs "
                "input_chars=%d output_chars=%d tokens=%s",
                resolved_model, call_category, elapsed,
                sum(_content_char_len(m.get("content", "")) for m in messages),
                len(result or ""),
                stream_result.total_tokens,
            )
            await self._publish_call(
                status="completed", session_key=session_key,
                call_category=call_category, model=resolved_model,
                latency_seconds=elapsed, total_tokens=stream_result.total_tokens,
            )
            return result

        except Exception as exc:
            elapsed = time.monotonic() - t0
            quota_gate.record_failure(exc, provider)
            logger.error("LLM dispatch failed: model=%s error=%s", resolved_model, exc)
            await _record_log(
                self.db,
                provider=provider,
                model=resolved_model,
                call_category=call_category,
                session_key=session_key,
                system_prompt=system_prompt,
                user_message=user_message,
                messages_json=messages_json,
                latency_seconds=elapsed,
                status="failed",
                error_message=str(exc),
                project_id=project_id,
                task_id=task_id,
                dispatch_id=dispatch_id,
                contact_id=contact_id,
            )
            await self._publish_call(
                status="failed", session_key=session_key,
                call_category=call_category, model=resolved_model,
                latency_seconds=elapsed, total_tokens=None,
                error_message=str(exc),
            )
            raise

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        call_category: str = "quick_prompt",
        session_key: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        dispatch_id: str | None = None,
        contact_id: str | None = None,
    ) -> AsyncIterator[str]:
        """Streaming chat completion with automatic logging."""
        resolved_model = self._resolve_model(model)
        provider = model_registry.provider_for(resolved_model)
        quota_gate.check(provider)
        service = self._get_service()

        system_prompt, user_message = _extract_from_messages(messages)
        messages_json = json.dumps(messages)

        stream_result = StreamResult()
        call_meta: dict[str, Any] = {}
        t0 = time.monotonic()
        ttft: float | None = None
        accumulated = ""

        try:
            async for chunk in service.chat_stream(
                messages=messages,
                model=resolved_model,
                temperature=temperature,
                max_tokens=max_tokens,
                stream_result=stream_result,
                call_meta=call_meta,
            ):
                if chunk:
                    if ttft is None:
                        ttft = time.monotonic() - t0
                    accumulated += chunk
                    yield chunk

            elapsed = time.monotonic() - t0

            log_id = await _record_log(
                self.db,
                provider=provider,
                model=resolved_model,
                call_category=call_category,
                session_key=session_key,
                system_prompt=system_prompt,
                user_message=user_message,
                messages_json=messages_json,
                response_text=accumulated,
                latency_seconds=elapsed,
                ttft_seconds=ttft,
                prompt_tokens=stream_result.prompt_tokens,
                completion_tokens=stream_result.completion_tokens,
                total_tokens=stream_result.total_tokens,
                cached_tokens=stream_result.cached_tokens,
                status="completed",
                project_id=project_id,
                task_id=task_id,
                dispatch_id=dispatch_id,
                contact_id=contact_id,
                generation_id=call_meta.get("generation_id"),
                reasoning_effort=call_meta.get("reasoning_effort"),
            )
            ls = getattr(self._get_settings(), "llm_streaming", None)
            if ls is None or getattr(ls, "trace_enabled", True):
                trace = _TraceWriter(
                    self.db, log_id=log_id, dispatch_id=dispatch_id,
                    session_key=session_key,
                    max_rows=getattr(ls, "trace_max_rows_per_call", 400)
                    if ls is not None else 400)
                for part in call_meta.get("reasoning_parts") or []:
                    trace.emit("reasoning_part", content=part["text"],
                               meta={"source": part["source"]})
                trace.emit(
                    "round_completed",
                    meta={"latency_seconds": round(elapsed, 3), "tool_calls": 0,
                          "prompt_tokens": stream_result.prompt_tokens,
                          "completion_tokens": stream_result.completion_tokens,
                          "cached_tokens": stream_result.cached_tokens,
                          "ttft_seconds": round(ttft, 3) if ttft else None})
                await trace.close()

            quota_gate.record_success(provider)
            logger.info(
                "LLM dispatch stream: model=%s category=%s latency=%.2fs ttft=%.2fs "
                "input_chars=%d output_chars=%d tokens=%s",
                resolved_model, call_category, elapsed, ttft or 0,
                sum(_content_char_len(m.get("content", "")) for m in messages),
                len(accumulated),
                stream_result.total_tokens,
            )
            await self._publish_call(
                status="completed", session_key=session_key,
                call_category=call_category, model=resolved_model,
                latency_seconds=elapsed, total_tokens=stream_result.total_tokens,
                ttft_seconds=ttft,
            )

        except Exception as exc:
            elapsed = time.monotonic() - t0
            quota_gate.record_failure(exc, provider)
            logger.error("LLM dispatch stream failed: model=%s error=%s", resolved_model, exc)
            await _record_log(
                self.db,
                provider=provider,
                model=resolved_model,
                call_category=call_category,
                session_key=session_key,
                system_prompt=system_prompt,
                user_message=user_message,
                messages_json=messages_json,
                response_text=accumulated,
                latency_seconds=elapsed,
                ttft_seconds=ttft,
                status="failed",
                error_message=str(exc),
                project_id=project_id,
                task_id=task_id,
                dispatch_id=dispatch_id,
                contact_id=contact_id,
            )
            await self._publish_call(
                status="failed", session_key=session_key,
                call_category=call_category, model=resolved_model,
                latency_seconds=elapsed, total_tokens=None,
                error_message=str(exc),
            )
            raise

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[Tool],
        *,
        model: str | None = None,
        max_iterations: int = 100,
        time_limit_seconds: float | None = None,
        call_category: str = "tool_call",
        session_key: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        dispatch_id: str | None = None,
        contact_id: str | None = None,
        budget_stats: dict[str, bool] | None = None,
        force_first_tool_choice: bool = False,
        reasoning_effort: str | None = None,
    ) -> str:
        """Chat with tool calling. Loops until LLM finishes or max iterations.

        The caller provides a list of Tool objects (created via @tool decorator)
        and this method handles the multi-turn tool call loop automatically.
        """
        resolved_model = self._resolve_model(model)
        provider = model_registry.provider_for(resolved_model)
        quota_gate.check(provider)
        service = self._get_service()

        system_prompt, user_message = _extract_from_messages(messages)

        openai_tools = [t.to_openai_format() for t in tools]
        tool_handlers = {t.name: t.handler for t in tools}
        tools_json = json.dumps(openai_tools) if openai_tools else None

        t0 = time.monotonic()
        original_len = len(messages)
        effort = _effective_reasoning_effort(
            resolved_model, reasoning_effort, self._get_settings())
        log_id = await _record_log(
            self.db,
            provider=provider, model=resolved_model,
            call_category=call_category, session_key=session_key,
            system_prompt=system_prompt, user_message=user_message,
            messages_json=json.dumps(_sanitize_for_json(messages)),
            tools_json=tools_json,
            status="running",
            project_id=project_id, task_id=task_id,
            dispatch_id=dispatch_id, contact_id=contact_id,
            reasoning_effort=effort,
        )
        await self._publish_call(
            status="running", session_key=session_key,
            call_category=call_category, model=resolved_model,
            latency_seconds=None, total_tokens=None,
            log_id=log_id,
        )
        ls = getattr(self._get_settings(), "llm_streaming", None)
        trace_writer = _TraceWriter(
            self.db, log_id=log_id, dispatch_id=dispatch_id,
            session_key=session_key,
            max_rows=getattr(ls, "trace_max_rows_per_call", 400)
            if ls is not None else 400,
        ) if (ls is None or getattr(ls, "trace_enabled", True)) and log_id else None
        try:
            stream_result = StreamResult()
            call_meta: dict[str, Any] = {}

            async def _on_iteration(msgs: list[dict[str, Any]]) -> None:
                await _record_log(self.db, log_id=log_id,
                    messages_json=json.dumps(_sanitize_for_json(msgs)),
                    status="running",
                )

            stream_cb = self._make_stream_callback(
                session_key=session_key, call_category=call_category,
                model=resolved_model, log_id=log_id, dispatch_id=dispatch_id)

            async def _on_round(
                iteration: int, response: Any, round_latency: float, usage: Any,
            ) -> None:
                if trace_writer:
                    await trace_writer.round_complete(
                        iteration, response, round_latency, usage)
                n_calls = sum(
                    1 for item in (getattr(response, "output", None) or [])
                    if getattr(item, "type", None) == "function_call")
                await stream_cb("round_finished", {
                    "iteration": iteration,
                    "latency_seconds": round(round_latency, 3),
                    "tool_calls": n_calls})

            result = await service.chat_with_tools(
                messages=messages,
                tools=openai_tools,
                tool_handlers=tool_handlers,
                model=resolved_model,
                max_iterations=max_iterations,
                time_limit_seconds=time_limit_seconds,
                stream_result=stream_result,
                on_tool_call=self._make_tool_callback(
                    session_key, call_category, log_id, dispatch_id, trace_writer),
                on_iteration_complete=_on_iteration,
                dispatch_id=dispatch_id,
                session_key=session_key,
                log_id=log_id,
                budget_stats=budget_stats,
                call_meta=call_meta,
                force_first_tool_choice=force_first_tool_choice,
                reasoning_effort=reasoning_effort,
                on_round_complete=_on_round,
                on_stream_event=stream_cb,
            )
            elapsed = time.monotonic() - t0
            if trace_writer:
                await trace_writer.close()

            trace: dict[str, Any] | None = None
            if dispatch_id:
                trace = _build_tool_trace(messages[original_len:])
                if trace is not None:
                    _dispatch_tool_trace[dispatch_id] = trace

            await _record_log(self.db, log_id=log_id,
                response_text=result,
                latency_seconds=elapsed,
                ttft_seconds=stream_result.ttft_seconds,
                prompt_tokens=stream_result.prompt_tokens,
                completion_tokens=stream_result.completion_tokens,
                total_tokens=stream_result.total_tokens,
                cached_tokens=stream_result.cached_tokens,
                messages_json=json.dumps(_sanitize_for_json(messages)),
                generation_id=call_meta.get("generation_id"),
                status="completed",
                tool_blocks_json=_serialize_trace_items(trace),
            )

            quota_gate.record_success(provider)
            logger.info(
                "LLM dispatch tools: model=%s category=%s latency=%.2fs "
                "tools=%d output_chars=%d tokens=%s",
                resolved_model, call_category, elapsed,
                len(tools), len(result),
                stream_result.total_tokens,
            )
            await self._publish_call(
                status="completed", session_key=session_key,
                call_category=call_category, model=resolved_model,
                latency_seconds=elapsed, total_tokens=stream_result.total_tokens,
            )
            return result

        except BaseException as exc:
            elapsed = time.monotonic() - t0
            is_cancel = isinstance(exc, asyncio.CancelledError)
            if not is_cancel:
                quota_gate.record_failure(exc, provider)
                logger.error("LLM dispatch tools failed: model=%s error=%s", resolved_model, exc)
            if dispatch_id:
                _dispatch_tool_trace.pop(dispatch_id, None)
            if trace_writer:
                trace_writer.emit(
                    "turn_note", content=f"turn failed: {str(exc)[:400]}",
                    meta={"cancelled": is_cancel})
                await trace_writer.close()
            cancel_reason = "server restart"
            if is_cancel and dispatch_id:
                try:
                    from server.services import backburner as _bb
                    cancel_reason = _bb.peek_cancel_reason(dispatch_id) or "server restart"
                except Exception:
                    pass
            await _record_log(self.db, log_id=log_id,
                latency_seconds=elapsed,
                messages_json=json.dumps(_sanitize_for_json(messages)),
                status="failed",
                error_message=f"Cancelled — {cancel_reason}" if is_cancel else str(exc),
            )
            await self._publish_call(
                status="failed", session_key=session_key,
                call_category=call_category, model=resolved_model,
                latency_seconds=elapsed, total_tokens=None,
                error_message=str(exc),
            )
            raise
