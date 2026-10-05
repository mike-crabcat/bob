"""OpenAI LLM service using the Responses API."""

from __future__ import annotations


import base64
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Callable, Awaitable
from httpx import Timeout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from server.context import AppContext
from server.services import model_registry
from server.services import tool_loop_folding
from server.services.base import BaseService
from server.services.tools import ImageInjection, VideoInjection

try:
    from openai import AsyncOpenAI
    import openai as _openai_module
except ImportError:
    AsyncOpenAI = None  # type: ignore[assignment, misc]
    _openai_module = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4.1-mini"


def _content_length(content: Any) -> int:
    """Return character length of message content, handling both str and list[dict]."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(part.get("text", "")) if isinstance(part, dict) else 0
            for part in content
        )
    return 0


def _output_items_to_dicts(items: list[Any]) -> list[dict[str, Any]]:
    """Convert Responses API output items to plain dicts for JSON serialization.

    Reasoning items serialize to id (+encrypted_content on OpenAI-direct)
    ONLY — deliberately NOT their summary/content text. These dicts ride the
    next round's request wire, where reasoning continuity needs the id/blob,
    while replaying GLM's raw thinking would bloat every subsequent round
    with ~1k-token content. Reasoning TEXT is captured for the trace table
    via ``_extract_reasoning`` on the raw response instead.
    """
    result: list[dict[str, Any]] = []
    for item in items:
        item_type = getattr(item, "type", None)
        if item_type == "function_call":
            result.append({
                "type": "function_call",
                "call_id": item.call_id,
                "name": item.name,
                "arguments": item.arguments,
            })
        elif item_type == "message":
            content = []
            if item.content:
                for c in item.content:
                    text = getattr(c, "text", "") or ""
                    # Strip any Hermes-style <tool_call> XML so it can't poison
                    # future turns via tool-block replay.
                    if "<tool_call>" in text:
                        text = _strip_hermes_tool_calls(text)
                    content.append({"type": c.type, "text": text})
            result.append({
                "type": "message",
                "role": item.role,
                "content": content,
            })
        elif item_type == "reasoning":
            d: dict[str, Any] = {"type": "reasoning"}
            item_id = getattr(item, "id", None)
            if item_id:
                d["id"] = item_id
            # summary MUST stay present (even []) when the item is replayed
            # in input — OpenAI 400s "Missing required parameter:
            # 'input[N].summary'" otherwise (live 2026-10-03). Serialized to
            # plain dicts; raw SDK objects only rode the wire by luck of the
            # old fallback serializer.
            d["summary"] = [
                {"type": getattr(s, "type", "summary_text"),
                 "text": getattr(s, "text", "") or ""}
                for s in (getattr(item, "summary", None) or [])
            ]
            # OpenAI-direct returns this unrequested (probe 2026-10-03);
            # passing it back is what preserves reasoning state across tool
            # rounds. OpenRouter/GLM items carry null.
            enc = getattr(item, "encrypted_content", None)
            if isinstance(enc, str) and enc:
                d["encrypted_content"] = enc
            result.append(d)
        else:
            # Fallback: try to serialize, skip if not possible
            try:
                d = {k: v for k, v in item.__dict__.items() if isinstance(v, (str, int, float, bool, list, dict, type(None)))}
                d.pop("status", None)
                result.append({"type": item_type, **d})
            except Exception:
                result.append({"type": str(item_type)})
    return result


# ──────────────────────────────────────────────────────────────────────
# Citation handling
#
# When web_search is enabled, OpenAI's Responses API wraps cited passages with
# private-use Unicode markers and emits the real URLs as `url_citation`
# annotations on the message item. The format in `output_text` is:
#
#     citeturn0search0turn0search10
#
# where  = block start,  = block end,  = ref separator.
# Without post-processing these markers leak into stored messages and outgoing
# WhatsApp replies as garbage.
#
# We replace each citation block with `[N]` markers and append a `Sources:`
# list of bare URLs (WhatsApp makes bare URLs clickable; markdown wouldn't
# render). When `ref_map` is empty (no annotations available — e.g. no
# web_search, or retroactive cleaning), citation blocks are stripped entirely.
# ──────────────────────────────────────────────────────────────────────

_REF_TOKEN = r"turn\d+(?:search|news|view)\d+"
_REF_TOKEN_RE = re.compile(_REF_TOKEN)

# OpenAI private-use Unicode markers
_CITE_BLOCK_START = ""
_CITE_BLOCK_END = ""
_REF_SEPARATOR = ""

# Match a complete or truncated OpenAI citation block. Non-greedy; stops at
# end marker, next block start, or end of string.
_CITATION_BLOCK_RE = re.compile(
    rf"{_CITE_BLOCK_START}cite(?:(?!{_CITE_BLOCK_START}).)*?(?:{_CITE_BLOCK_END}|(?={_CITE_BLOCK_START})|$)",
    re.DOTALL,
)


def _extract_ref_map_from_response(response: Any) -> dict[str, str]:
    """Build a `{ref_token: url}` map from url_citation annotations.

    Each annotation's `start_index/end_index` points into the message item's
    content text where the citation placeholder lives. We extract any ref
    tokens in that range and map them to the annotation's URL.
    """
    ref_map: dict[str, str] = {}
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "message":
            continue
        for content in (getattr(item, "content", None) or []):
            text = getattr(content, "text", "") or ""
            for ann in (getattr(content, "annotations", None) or []):
                if getattr(ann, "type", None) != "url_citation":
                    continue
                start = getattr(ann, "start_index", None)
                end = getattr(ann, "end_index", None)
                cit = getattr(ann, "url_citation", None)
                if cit is None or start is None or end is None:
                    continue
                if hasattr(cit, "url"):
                    url = getattr(cit, "url", None)
                elif isinstance(cit, dict):
                    url = cit.get("url")
                else:
                    url = None
                if not url:
                    continue
                if 0 <= start < end <= len(text):
                    for ref in _REF_TOKEN_RE.findall(text[start:end]):
                        ref_map[ref] = url
    return ref_map


def _render_citations(text: str, ref_map: dict[str, str]) -> str:
    """Replace citation blocks with `[N]` markers and append a Sources list.

    URL deduplication: each unique URL gets one number, assigned in first-encounter
    order. Stray Unicode markers are stripped at the end so text never leaks
    private-use chars even if a block was malformed.
    """
    url_to_idx: dict[str, int] = {}
    sources: list[tuple[int, str]] = []

    def replace(m: re.Match) -> str:
        block = m.group(0)
        refs = _REF_TOKEN_RE.findall(block)
        if not refs or not ref_map:
            return ""
        markers: list[str] = []
        for ref in refs:
            if ref not in ref_map:
                continue
            url = ref_map[ref]
            if url not in url_to_idx:
                url_to_idx[url] = len(url_to_idx) + 1
                sources.append((url_to_idx[url], url))
            markers.append(f"[{url_to_idx[url]}]")
        return "".join(markers) if markers else ""

    cleaned = _CITATION_BLOCK_RE.sub(replace, text)

    # Strip any stray markers that survived (malformed/truncated blocks)
    for marker in (_CITE_BLOCK_START, _CITE_BLOCK_END, _REF_SEPARATOR):
        cleaned = cleaned.replace(marker, "")

    if sources:
        cleaned = cleaned.rstrip() + "\n\nSources:\n" + "\n".join(
            f"[{n}] {url}" for n, url in sources
        )
    return cleaned


def _response_text_with_citations(response: Any) -> str:
    """Return response.output_text with citation placeholders rendered as Sources."""
    text = getattr(response, "output_text", "") or ""
    if not text:
        return ""
    ref_map = _extract_ref_map_from_response(response)
    return _render_citations(text, ref_map)


# Hermes-style <tool_call> XML that some models emit as text instead of using
# the native function_call API. We recover these by parsing + dispatching the
# named handler, so the user-visible reply isn't lost.
_HERMES_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*<tool_name>\s*(?P<name>[^<\s]+)\s*</tool_name>\s*"
    r"<parameters>\s*(?P<args>\{.*?\})\s*</parameters>\s*</tool_call>",
    re.DOTALL,
)


def _parse_hermes_tool_calls(text: str) -> list[tuple[str, dict[str, Any]]]:
    """Extract (tool_name, args) pairs from Hermes-style XML in text.

    Strips a leading ``functions.`` (or similar) namespace prefix on the tool
    name, since the model sometimes hallucinates ``functions.send_whatsapp_message``
    when the actual registered handler key is ``send_whatsapp_message``.
    """
    calls: list[tuple[str, dict[str, Any]]] = []
    if "<tool_call>" not in text:
        return calls
    for match in _HERMES_TOOL_CALL_RE.finditer(text):
        raw_name = match.group("name").strip()
        name = raw_name.split(".", 1)[1] if raw_name.startswith("functions.") else raw_name
        try:
            args = json.loads(match.group("args"))
        except json.JSONDecodeError:
            continue
        if isinstance(args, dict):
            calls.append((name, args))
    return calls


def _strip_hermes_tool_calls(text: str) -> str:
    """Remove <tool_call>...</tool_call> blocks and trailing 'Done.' residue."""
    if "<tool_call>" not in text:
        return text
    cleaned = _HERMES_TOOL_CALL_RE.sub("", text)
    cleaned = strip_leaked_tool_xml(cleaned)
    # Models often append "Done." or "Done!" after the XML block.
    cleaned = re.sub(r"\s*\b[Dd]one!?\.?\s*", "", cleaned)
    return cleaned.strip()


# Any <tool_call> span regardless of inner dialect. The Hermes shape above is
# recovered as a real call; other dialects (GLM's arg_key/arg_value variant)
# can only be stripped, never delivered raw — and upstream API parsing can eat
# just the opening half of a malformed span, leaving an orphaned tail that
# leaked verbatim into the Bob-management group (2026-09-04).
_TOOL_CALL_ANY_SPAN_RE = re.compile(r"<tool_call\b[^>]*>.*?(?:</tool_call>|\Z)", re.DOTALL)
_LEAKED_TOOL_TAG_RE = re.compile(r"</?(?:tool_call|tool_name|parameters|arg_key|arg_value)\b[^>]*/?>")


def strip_leaked_tool_xml(text: str) -> str:
    """Remove tool-call markup a model leaked into message text.

    Full <tool_call> spans go entirely (a malformed call attempt is not
    prose). Orphaned tags left behind by upstream half-parsing are removed
    while the text between them is kept:
    "</arg_key><arg_value>Objective complete…</arg_value></tool_call>"
    cleans to "Objective complete…".
    """
    if not text:
        return text
    cleaned = _TOOL_CALL_ANY_SPAN_RE.sub(" ", text)
    cleaned = _LEAKED_TOOL_TAG_RE.sub(" ", cleaned)
    if cleaned != text:
        cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
    return cleaned


def strip_citation_markers(text: str) -> str:
    """Remove OpenAI web_search citation blocks from arbitrary text.

    Use this on LLM-produced text that bypasses `output_text` — e.g. tool-call
    arguments for send_message-style tools. Without a ref_map we can't render
    `[N]` markers or a Sources list, so blocks are dropped entirely.
    """
    if not text:
        return text
    return _render_citations(text, {})


# Self-wrap budget nudges (settings.self_wrap). Soft nudge as the turn nears
# its time or iteration budget, final instruction on the forced wrap-up round.
# Both are stripped from the messages list before run_turn returns so
# they never persist into conversation history.
_SELF_WRAP_NUDGE = (
    "You are close to this turn's budget (time or tool calls). Stop starting "
    "new work: finish the current step, then reply to the user now with what "
    "you have, noting anything left unfinished.")
_SELF_WRAP_FINAL = (
    "This turn's budget is exhausted. Do not call any more tools. Reply to "
    "the user right now with a short summary of what you found or did, and "
    "say plainly what is left undone.")
# Send-tool variants (2026-09-14 crypto-report incident): on channels where
# delivery is a tool call, "reply to the user" in plain text delivers
# nothing — the nudge must say DELIVER VIA THE SEND TOOL while tools are
# still available, and the tools-stripped final round must write the
# message the runner/rescue will deliver on the model's behalf.
_SELF_WRAP_NUDGE_SEND = (
    "You are close to this turn's budget (time or tool calls). Stop starting "
    "new work and finish now: write your final reply as your closing text — "
    "it is delivered automatically. Note anything left unfinished inside it, "
    f"and register genuinely-promised work with add_goal(profile='promise') first. If "
    "silence is correct, finish with the exact text NO_REPLY.")
_SELF_WRAP_FINAL_SEND = (
    "This turn's budget is exhausted and tools are now disabled. Write the "
    "final message for the user as your reply — it will be delivered for "
    "you. Keep it short: what you found or did, and what is left undone.")
_LEGACY_TIME_STOP = (
    "Stopped at the turn's wall-clock budget — work done so far is complete, "
    "remaining steps were skipped.")
_LEGACY_ITER_STOP = "Max tool call iterations reached."

_WRAP_TEXTS: tuple[str, ...] = (
    _SELF_WRAP_NUDGE, _SELF_WRAP_FINAL,
    _SELF_WRAP_NUDGE_SEND, _SELF_WRAP_FINAL_SEND,
)


def _has_send_tool(tools: list[Any]) -> bool:
    """True when the turn's tool set contains a delivery tool — the wrap
    texts must then talk in terms of calling it, not 'replying' in text.
    Handles both Tool objects and openai-format dicts."""
    for t in tools or []:
        name = getattr(t, "name", None)
        if name is None and isinstance(t, dict):
            name = (t.get("function") or {}).get("name") or t.get("name")
        if name and (str(name).startswith("send_") or name == "email_reply"):
            return True
    return False


def _strip_wrap_nudges(messages: list[dict[str, Any]]) -> None:
    """Remove injected budget nudges in place (turn-scoped, never persisted)."""
    messages[:] = [
        m for m in messages
        if not (isinstance(m, dict)
                and m.get("content") in _WRAP_TEXTS)
    ]


@dataclass
class StreamResult:
    """Stats from a completed streaming call."""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None
    latency_seconds: float | None = None
    ttft_seconds: float | None = None
    finish_reason: str | None = None


# Module-level client cache so httpx reuses TCP connections across requests.
# Keyed (api_key, base_url) so multiple providers (OpenAI, OpenRouter) coexist
# without recreating clients on every alternating call.
_clients: dict[tuple[str, str], Any] = {}


def _routing_extra(settings: Any, resolved_model: str) -> dict[str, Any]:
    """OpenRouter routing constraint (2026-09-26): the default router serves
    glm-5.3-flash (and other routed slugs) from a 33-endpoint pool that
    includes fp4/nvfp4 and undeclared-quant hosts — the fp4 ones are the
    cheapest, so they win price-weighted routing often. settings.openrouter
    .quantizations (comma list, "" or "off" to disable) becomes
    provider.quantizations, keeping competition among the allowed-precision
    hosts and excluding endpoints that don't declare a listed quant. Sent via
    extra_body on every request shape (chat/completions and responses both
    honour it; verified live 2026-09-26). Non-OpenRouter models get {}."""
    from server.services import model_registry
    if model_registry.provider_for(resolved_model) != model_registry.PROVIDER_OPENROUTER:
        return {}
    raw = getattr(settings.openrouter, "quantizations", "") or ""
    quants = [q.strip() for q in raw.split(",") if q.strip()]
    if not quants or quants == ["off"]:
        return {}
    return {"extra_body": {"provider": {"quantizations": quants}}}


def _note_generation(call_meta: dict | None, response: Any) -> None:
    """Stash the OpenRouter generation id on the caller's out-param dict —
    the Responses API returns gen-… ids in-band but NOT the serving
    provider; the attribution sweep resolves provider/quant from the
    generation endpoint afterwards (heartbeat OpenRouterAttributionTask).
    A tool loop makes one request per iteration; the LAST round's id wins."""
    if call_meta is not None:
        gen_id = getattr(response, "id", None)
        if gen_id:
            call_meta["generation_id"] = gen_id


def _get_cached_client(
    api_key: str, base_url: str, *, default_headers: dict[str, str] | None = None,
) -> Any:
    cache_key = (api_key, base_url)
    if cache_key in _clients:
        return _clients[cache_key]
    if AsyncOpenAI is None:
        raise RuntimeError("openai SDK is not installed.")
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=Timeout(300.0, connect=30.0),
        default_headers=default_headers,
    )
    _clients[cache_key] = client
    logger.info("OpenAI-compatible client created for base_url=%s", base_url)
    return client


def _model_skips_temperature(model: str) -> bool:
    """Return True for models that don't accept the temperature parameter."""
    return any(model.startswith(p) for p in ("gpt-5.5", "gpt-5.6", "gpt-6", "o1", "o3", "o4"))


def _accepts_reasoning_effort(model: str) -> bool:
    """True for models that take the reasoning-effort hint.

    OpenAI reasoning models (gpt-5.x, o-series) and anything served through
    OpenRouter, whose gateway maps the unified effort param per provider.
    Without the hint a thinking model (GLM-5.3 via OpenRouter) reasons at
    full budget, which alone can exhaust a small max_output_tokens cap and
    return empty content.
    """
    return (_model_skips_temperature(model)
            or model_registry.provider_for(model) == model_registry.PROVIDER_OPENROUTER)


def _effective_reasoning_effort(model: str, explicit: str | None, settings: Any) -> str | None:
    """Merge the per-call effort hint with the models.yaml per-model default.

    An explicit caller hint wins (background passes pin their own); otherwise
    the ``effort:`` map in models.yaml supplies the default, so thinking
    models can be dialled down globally without touching call sites. Both
    paths are gated on the model accepting the hint.
    """
    if not _accepts_reasoning_effort(model):
        return None
    if explicit is not None:
        return explicit
    config_dir = getattr(settings, "config_dir", None)
    if config_dir is None:
        return None
    return model_registry.effort_defaults(config_dir).get(model)


def _request_reasoning(
    model: str, explicit_effort: str | None, settings: Any,
    *, max_output_tokens: int | None = None,
) -> dict[str, Any] | None:
    """Build the ``reasoning`` request param (2026-10-03 trace uplift).

    Effort rides whenever the model accepts the hint (unchanged). The
    ``summary: "auto"`` key asks for readable reasoning digests — accepted by
    both rails (probe 2026-10-03: OpenAI-direct fills item.summary when the
    model thinks hard enough; OpenRouter/GLM ignores it and streams raw
    reasoning_text instead — both shapes are extracted downstream). Summary
    tokens are billed as output, so capped background passes
    (max_output_tokens below the floor) keep their whole budget for content.
    """
    effort = _effective_reasoning_effort(model, explicit_effort, settings)
    if not _accepts_reasoning_effort(model):
        return None
    reasoning: dict[str, Any] = {}
    if effort is not None:
        reasoning["effort"] = effort
    # Summary rides on its own when no effort is pinned: flagship models with
    # no models.yaml entry still get summaries at their default effort.
    ls = getattr(settings, "llm_streaming", None)
    summary_on = getattr(ls, "summary_enabled", False) if ls is not None else False
    floor = getattr(ls, "summary_min_output_tokens", 3000) if ls is not None else 3000
    if summary_on and (max_output_tokens is None or max_output_tokens >= floor):
        reasoning["summary"] = "auto"
    return reasoning or None


def _extract_reasoning(response: Any) -> list[dict[str, Any]]:
    """Pull reasoning text out of a completed Responses API response.

    Two dialects (probe 2026-10-03, docs/llm-streaming-probe.md): OpenAI-direct
    fills ``item.summary`` (summary_text parts); OpenRouter/GLM maps native
    reasoning into ``item.content`` (reasoning_text parts). Returns a list of
    ``{"source": "summary"|"raw", "text": str}`` in output order.

    Streamed rounds may carry the text ONLY in the event stream — some
    OpenRouter hosts return ``content: null`` on the reasoning item inside
    ``response.completed`` (live 2026-10-03: routine turn streamed 2587 chars
    of reasoning events, completed item empty, trace got 0 rows). ``_round``
    therefore accumulates the ``reasoning_*`` done-events and exposes them as
    ``response.stream_reasoning``; used as a fallback ONLY when the completed
    response itself carries nothing (never merged — hosts that do fill
    content would double-count).
    """
    parts: list[dict[str, Any]] = []

    def _add(source: str, obj: Any) -> None:
        text = (getattr(obj, "text", None) or "").strip()
        if text:
            parts.append({"source": source, "text": text})

    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) != "reasoning":
            continue
        for s in (getattr(item, "summary", None) or []):
            _add("summary", s)
        for c in (getattr(item, "content", None) or []):
            if getattr(c, "type", None) == "reasoning_text":
                _add("raw", c)
    if parts:
        return parts
    return list(getattr(response, "stream_reasoning", None) or [])


class OpenAIService(BaseService):
    """LLM reasoning through OpenAI Responses API."""

    @property
    def client(self) -> Any:
        settings = self._get_settings().openai
        if not settings.enabled:
            raise RuntimeError("OpenAI is not configured. Set BOB_OPENAI_API_KEY.")
        return _get_cached_client(settings.api_key, settings.base_url)

    def _client_for(self, model: str) -> Any:
        """Return the API client serving this model — OpenRouter for
        vendor-qualified slugs, direct OpenAI otherwise (model_registry)."""
        if model_registry.provider_for(model) == model_registry.PROVIDER_OPENROUTER:
            settings = self._get_settings().openrouter
            if not settings.enabled:
                raise RuntimeError(
                    "OpenRouter is not configured. Create the API key file "
                    "(BOB_OPENROUTER_API_KEY_FILE, default ~/config/openrouter_api_key).")
            return _get_cached_client(
                settings.api_key, settings.base_url,
                default_headers={"X-Title": "bob"})
        return self.client

    @property
    def _web_search_tool(self) -> dict[str, Any] | None:
        if self._get_settings().openai.web_search_enabled:
            return {"type": "web_search", "search_context_size": "medium"}
        return None

    def _merge_tools(
        self, tools: list[dict[str, Any]] | None = None, *, model: str | None = None,
    ) -> list[dict[str, Any]]:
        """Merge caller-provided tools with built-in tools like web_search.

        web_search is an OpenAI-native Responses built-in and is dropped for
        OpenRouter-served models, which don't accept the tool type.
        """
        merged: list[dict[str, Any]] = []
        resolved = model or self._get_settings().openai.default_model
        if (self._web_search_tool
                and model_registry.provider_for(resolved) == model_registry.PROVIDER_OPENAI):
            merged.append(self._web_search_tool)
        if tools:
            merged.extend(tools)
        return merged

    def _common_request_kwargs(
        self, model: str, *, temperature: float | None = None,
        max_tokens: int | None = None, reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        """The request-kwargs core every Responses call shares (2026-10-03
        cleanup — was duplicated across the old chat-family methods):
        model, temperature (when the model accepts it), max_output_tokens,
        the reasoning param (effort + summary, see _request_reasoning), and
        the OpenRouter routing constraint. Tools/stream/tool_choice stay at
        the call sites — they genuinely differ per shape."""
        kwargs: dict[str, Any] = {"model": model}
        if temperature is not None and not _model_skips_temperature(model):
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_output_tokens"] = max_tokens
        reasoning = _request_reasoning(
            model, reasoning_effort, self._get_settings(),
            max_output_tokens=max_tokens)
        if reasoning is not None:
            kwargs["reasoning"] = reasoning
        kwargs.update(_routing_extra(self._get_settings(), model))
        return kwargs

    async def prompt(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        stream_result: StreamResult | None = None,
        call_meta: dict | None = None,
    ) -> str:
        """One LLM round: prompt in, complete text back.

        Single-round callers (memory passes, dream, judges, probes) — the
        degenerate case of run_turn. Rides the SAME streamed transport via
        _round (2026-10-04 unification): buffered only under the
        BOB_LLM_STREAMING kill switch / rail fallback, reasoning captured
        through the same extraction, llm.stream deltas flow to the bus so
        background passes are watchable live. Returns the final text with
        citations rendered."""
        resolved_model = model or self._get_settings().openai.default_model
        kwargs: dict[str, Any] = dict(self._common_request_kwargs(
            resolved_model, temperature=temperature, max_tokens=max_tokens,
            reasoning_effort=reasoning_effort))
        if "reasoning" in kwargs and call_meta is not None:
            call_meta.setdefault(
                "reasoning_effort", kwargs["reasoning"].get("effort"))

        tools = self._merge_tools(model=resolved_model)
        if tools:
            kwargs["tools"] = tools

        t0 = time.monotonic()
        try:
            response = await self._round(
                _video_safe_wire(messages, dispatch_id=None, model=resolved_model),
                kwargs)
            elapsed = time.monotonic() - t0
            content = _response_text_with_citations(response)
            usage = getattr(response, "usage", None)
            _note_generation(call_meta, response)
            if call_meta is not None:
                call_meta["reasoning_parts"] = _extract_reasoning(response)

            cached_tokens = self._extract_cached_tokens(usage)

            if stream_result is not None:
                stream_result.prompt_tokens = usage.input_tokens if usage else None
                stream_result.completion_tokens = usage.output_tokens if usage else None
                stream_result.total_tokens = usage.total_tokens if usage else None
                stream_result.cached_tokens = cached_tokens
                stream_result.latency_seconds = elapsed

            logger.info(
                "OpenAI prompt: model=%s latency=%.2fs "
                "input_tokens=%s output_tokens=%s total_tokens=%s "
                "cached_tokens=%s input_chars=%d output_chars=%d",
                resolved_model, elapsed,
                usage.input_tokens if usage else None,
                usage.output_tokens if usage else None,
                usage.total_tokens if usage else None,
                cached_tokens,
                sum(_content_length(m.get("content", "")) for m in messages),
                len(content),
            )
            return content
        except Exception as e:
            elapsed = time.monotonic() - t0
            logger.error("OpenAI prompt failed: model=%s latency=%.2fs error=%s", resolved_model, elapsed, e)
            _raise_openai_error(e)

    async def run_turn(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_handlers: dict[str, Callable[..., Awaitable[str | ImageInjection | VideoInjection]]],
        *,
        model: str | None = None,
        max_iterations: int = 100,
        time_limit_seconds: float | None = None,
        stream_result: StreamResult | None = None,
        on_tool_call: Callable[[str, dict, str], Awaitable[None]] | None = None,
        on_iteration_complete: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
        dispatch_id: str | None = None,
        session_key: str | None = None,
        log_id: str | None = None,
        budget_stats: dict[str, bool] | None = None,
        call_meta: dict | None = None,
        force_first_tool_choice: bool = False,
        reasoning_effort: str | None = None,
        on_round_complete: Callable[[int, Any, float, Any], Awaitable[None]] | None = None,
        on_stream_event: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> str:
        """Run one agentic turn via the Responses API: the tool loop.

        Loops: send input → check for function_call items → execute → feed back.
        Returns the final text response. ``time_limit_seconds`` is a wall-clock
        budget checked before each iteration (an in-flight LLM call and the
        tool round that started it always complete) — used by routine
        dispatches so an hourly bulletin can't become a 10-minute tool odyssey.

        Budget exhaustion is a two-stage self-wrap (settings.self_wrap): a
        soft one-shot nudge injected near the budget (duration fraction or
        iteration margin) asking the model to wrap up in its own words, then
        a forced final round with tools stripped at the deadline/iteration
        cap. With self_wrap disabled the legacy canned-string stop applies.
        Wrap texts are send-tool aware: when the tool set contains a delivery
        tool, the nudge says to DELIVER via it (text output alone reaches
        nobody) and the final round writes text the caller is expected to
        deliver on the model's behalf.

        ``budget_stats`` (optional out-param) gets ``hit_wall_clock`` /
        ``hit_iteration_cap`` set when the respective budget ended the loop —
        callers use it to decide cutoff rescues.

        ``on_round_complete`` (2026-10-03 trace uplift) fires after EVERY
        round's response arrives — tool rounds and the final text round alike
        — with ``(iteration, response, round_latency_seconds, usage)``. It is
        the transport-agnostic seam the trace writer hangs off: reasoning
        extraction reads the raw response (summary and raw-thinking dialects
        both), per-round latency/tokens ride the call. Exceptions inside the
        callback are swallowed like the other callbacks — tracing must never
        kill a turn.
        """
        resolved_model = model or self._get_settings().openai.default_model
        merged_tools = self._merge_tools(tools, model=resolved_model)
        request_kwargs: dict[str, Any] = {
            "tools": merged_tools,
            **self._common_request_kwargs(
                resolved_model, reasoning_effort=reasoning_effort),
        }
        # Eval verification aid (Phase 0, 2026-09-19): GLM narrates tool use
        # instead of calling it on short minimal-context prompts ("The agenda
        # has been updated" with zero calls — the send-tool-skip quirk's
        # general shape). Forcing tool_choice on the FIRST request only makes
        # tool_call_made checks deterministic; later rounds stay auto so the
        # loop converges instead of calling tools forever.
        if force_first_tool_choice and merged_tools:
            request_kwargs["tool_choice"] = "required"
        if "reasoning" in request_kwargs and call_meta is not None:
            call_meta.setdefault(
                "reasoning_effort", request_kwargs["reasoning"].get("effort"))
        t0 = time.monotonic()
        deadline = t0 + time_limit_seconds if time_limit_seconds is not None else None

        wrap = self._get_settings().self_wrap
        nudge_at = (t0 + time_limit_seconds * wrap.duration_fraction
                    if time_limit_seconds is not None else None)
        nudged = False
        send_tool_turn = _has_send_tool(tools)

        # Tool-loop folding (2026-09-10): the caller's history is exactly
        # messages[:base_len]; everything after is this loop's transcript.
        # Big loops re-sent that whole prefix on every iteration — fold aged
        # oversized results in place, and when the history itself is big,
        # intermediate iterations carry a trimmed wire view of it.
        tl = getattr(self._get_settings(), "tool_loop", None)
        base_len = len(messages)
        use_view = False
        if tl is not None and tl.folding_enabled:
            use_view = tool_loop_folding.history_chars(
                messages, base_len) > tl.history_view_trigger_chars
        fold_dropped = 0
        fold_count = 0

        total_input = total_output = total_total = 0
        total_cached = 0
        first_delta_at: float | None = None

        async def _round_events(kind: str, data: dict[str, Any]) -> None:
            """Stamp TTFT on the first delta, then hand off to the caller's
            stream callback (no-op when the caller passed none)."""
            nonlocal first_delta_at
            if first_delta_at is None and kind in ("text_delta", "reasoning_delta"):
                first_delta_at = time.monotonic()
            if on_stream_event is not None:
                await on_stream_event(kind, data)

        try:
            for iteration in range(max_iterations):
                if iteration > 0:
                    # forced-choice applies to the FIRST request only —
                    # leaving it set makes every round call a tool and the
                    # loop never converges (found live 2026-09-19: the probe
                    # called update_agenda until the wall clock killed it).
                    request_kwargs.pop("tool_choice", None)
                now = time.monotonic()
                if (wrap.enabled and not nudged
                        and ((nudge_at is not None and now >= nudge_at)
                             or iteration >= max_iterations - wrap.iteration_margin)):
                    messages.append({"role": "system", "content": (
                        _SELF_WRAP_NUDGE_SEND if send_tool_turn
                        else _SELF_WRAP_NUDGE)})
                    nudged = True
                    logger.info(
                        "OpenAI self-wrap nudge: model=%s iteration=%d "
                        "elapsed=%.1fs budget=%s dispatch_id=%s session_key=%s",
                        resolved_model, iteration, now - t0,
                        f"{time_limit_seconds}s" if time_limit_seconds else
                        f"{max_iterations} iters", dispatch_id, session_key)

                if deadline is not None and now >= deadline:
                    logger.warning(
                        "OpenAI tool call hit wall-clock limit: model=%s limit=%ss "
                        "iterations=%d elapsed=%.1fs dispatch_id=%s session_key=%s",
                        resolved_model, time_limit_seconds, iteration,
                        now - t0, dispatch_id, session_key)
                    if budget_stats is not None:
                        budget_stats["hit_wall_clock"] = True
                    if not wrap.enabled:
                        return _LEGACY_TIME_STOP
                    return await self._forced_wrapup(
                        messages, resolved_model, request_kwargs,
                        dispatch_id=dispatch_id, session_key=session_key,
                        fallback=_LEGACY_TIME_STOP,
                        base_len=base_len if use_view else None,
                        history_keep=tl.history_view_keep if tl is not None else 20,
                        send_tool_turn=send_tool_turn,
                        on_stream_event=_round_events)

                round_t0 = time.monotonic()
                await _round_events("round_started", {"iteration": iteration})
                response = await self._round(
                    _video_safe_wire(
                        tool_loop_folding.iteration_view(
                            messages, base_len,
                            history_keep=(tl.history_view_keep
                                          if tl is not None else 20))
                        if use_view else messages,
                        dispatch_id=dispatch_id, model=resolved_model),
                    request_kwargs,
                    on_stream_event=_round_events,
                    iteration=iteration,
                    dispatch_id=dispatch_id,
                )
                round_latency = time.monotonic() - round_t0
                _note_generation(call_meta, response)

                usage = getattr(response, "usage", None)
                if usage:
                    total_input  += usage.input_tokens or 0
                    total_output += usage.output_tokens or 0
                    total_total  += usage.total_tokens or 0
                    total_cached += self._extract_cached_tokens(usage) or 0

                if on_round_complete is not None:
                    try:
                        await on_round_complete(iteration, response, round_latency, usage)
                    except Exception:
                        logger.warning(
                            "on_round_complete callback failed: dispatch_id=%s "
                            "session_key=%s iteration=%d",
                            dispatch_id, session_key, iteration, exc_info=True)

                # Check for function calls in output
                function_calls = [
                    item for item in response.output
                    if getattr(item, "type", None) == "function_call"
                ]

                if not function_calls:
                    elapsed = time.monotonic() - t0
                    content = _response_text_with_citations(response)
                    if not content:
                        # After tool iterations an empty final message is the
                        # normal shape (reply was delivered via send_message
                        # etc.) — only a turn with NO tool calls going silent
                        # is noteworthy.
                        _empty_log = logger.debug if iteration > 0 else logger.warning
                        _empty_log(
                            "OpenAI empty response: model=%s status=%s output_types=%s refusal=%s",
                            resolved_model,
                            getattr(response, "status", None),
                            [getattr(item, "type", None) for item in (response.output or [])],
                            getattr(response, "refusal", None) or next(
                                (getattr(item, "refusal", None) for item in (response.output or [])
                                 if getattr(item, "type", None) == "message"), None
                            ),
                        )
                    # Recover Hermes-style <tool_call> XML the model emitted as text
                    # instead of using the native function_call API. Parse, execute,
                    # and return the residual text so the user-visible reply isn't
                    # lost and the XML doesn't get persisted into future turns.
                    hermes_calls = _parse_hermes_tool_calls(content) if content else []
                    if hermes_calls:
                        recovered_names: list[str] = []
                        for hc_name, hc_args in hermes_calls:
                            handler = tool_handlers.get(hc_name)
                            if handler is None:
                                logger.warning(
                                    "Hermes tool call referenced unknown tool: tool=%s "
                                    "dispatch_id=%s session_key=%s log_id=%s",
                                    hc_name, dispatch_id, session_key, log_id,
                                )
                                continue
                            try:
                                hc_result = await handler(**hc_args)
                                recovered_names.append(hc_name)
                                if on_tool_call:
                                    try:
                                        summary = (
                                            hc_result.text[:200]
                                            if isinstance(hc_result, (ImageInjection, VideoInjection))
                                            else hc_result[:200]
                                        )
                                        await on_tool_call(hc_name, hc_args, summary)
                                    except Exception:
                                        pass
                            except Exception as hc_exc:
                                logger.error(
                                    "Hermes tool call failed: tool=%s dispatch_id=%s "
                                    "session_key=%s args=%s error=%s",
                                    hc_name, dispatch_id, session_key,
                                    json.dumps(hc_args, default=str)[:500], hc_exc,
                                    exc_info=True,
                                )
                        if recovered_names:
                            logger.info(
                                "Recovered %d Hermes tool call(s) from text: model=%s "
                                "dispatch_id=%s session_key=%s tools=%s",
                                len(recovered_names), resolved_model, dispatch_id,
                                session_key, recovered_names,
                            )
                            content = _strip_hermes_tool_calls(content)
                    logger.info(
                        "OpenAI tool call finished: model=%s iterations=%d latency=%.2fs "
                        "tool_calls_in_turn=%d tokens=%d (in=%d out=%d cached=%d)",
                        resolved_model, iteration + 1, elapsed,
                        iteration,
                        total_total, total_input, total_output, total_cached,
                    )
                    view_live = use_view and base_len > (
                        tl.history_view_keep if tl is not None else 20) + 1
                    if fold_dropped or view_live:
                        logger.info(
                            "tool-loop folding: dropped %d chars over %d tool "
                            "round(s); history view %s (%d history msgs → keep "
                            "%d) dispatch_id=%s session_key=%s",
                            fold_dropped, fold_count,
                            "on" if view_live else "off", base_len,
                            tl.history_view_keep if tl is not None else 20,
                            dispatch_id, session_key)
                    if stream_result is not None:
                        stream_result.prompt_tokens = total_input
                        stream_result.completion_tokens = total_output
                        stream_result.total_tokens = total_total
                        stream_result.cached_tokens = total_cached
                        stream_result.latency_seconds = elapsed
                        if first_delta_at is not None:
                            stream_result.ttft_seconds = first_delta_at - t0
                    return content

                # Append output items (including reasoning) to messages for context
                messages.extend(_output_items_to_dicts(response.output))

                # Execute each function call and append results
                for fc in function_calls:
                    handler = tool_handlers.get(fc.name)
                    tool_args: dict = {}
                    if handler is None:
                        result = f"Error: unknown tool '{fc.name}'"
                        logger.error(
                            "Unknown tool requested: tool=%s call_id=%s dispatch_id=%s "
                            "session_key=%s log_id=%s iteration=%d",
                            fc.name, fc.call_id, dispatch_id, session_key, log_id, iteration,
                        )
                    else:
                        try:
                            tool_args = json.loads(fc.arguments)
                            result = await handler(**tool_args)
                        except Exception as e:
                            result = f"Error: {e}"
                            logger.error(
                                "Tool call failed: tool=%s call_id=%s dispatch_id=%s "
                                "session_key=%s log_id=%s iteration=%d args=%s error=%s",
                                fc.name, fc.call_id, dispatch_id, session_key, log_id,
                                iteration, json.dumps(tool_args, default=str)[:500], e,
                                exc_info=True,
                            )

                    messages.extend(_tool_result_messages(
                        result, fc.call_id,
                        video_supported=model_registry.supports_video(
                            self._get_settings().config_dir, resolved_model),
                    ))

                    if on_tool_call:
                        try:
                            summary = (
                                result.text[:200]
                                if isinstance(result, (ImageInjection, VideoInjection))
                                else result[:200]
                            )
                            await on_tool_call(fc.name, tool_args, summary)
                        except Exception:
                            pass

                logger.info(
                    "run_turn: iteration=%d function_calls=%d",
                    iteration + 1, len(function_calls),
                )

                if tl is not None and tl.folding_enabled:
                    tool_loop_folding.fold_aged_image_outputs(
                        messages, keep_last=3)
                    dropped = tool_loop_folding.fold_aged_tool_outputs(
                        messages,
                        keep_last=tl.fold_keep_last,
                        size_threshold=tl.fold_size_threshold,
                        head_chars=tl.fold_head_chars,
                        tail_chars=tl.fold_tail_chars)
                    if dropped:
                        fold_dropped += dropped
                        fold_count += 1

                if on_iteration_complete:
                    try:
                        await on_iteration_complete(messages)
                    except Exception:
                        pass

            logger.warning(
                "OpenAI tool call hit max iterations: model=%s max=%d",
                resolved_model, max_iterations)
            if budget_stats is not None:
                budget_stats["hit_iteration_cap"] = True
            if not wrap.enabled:
                return _LEGACY_ITER_STOP
            return await self._forced_wrapup(
                messages, resolved_model, request_kwargs,
                dispatch_id=dispatch_id, session_key=session_key,
                fallback=_LEGACY_ITER_STOP,
                base_len=base_len if use_view else None,
                history_keep=tl.history_view_keep if tl is not None else 20,
                send_tool_turn=send_tool_turn,
                on_stream_event=_round_events)
        finally:
            # Budget nudges are turn-scoped guidance — never persist them
            # into the conversation history the caller keeps.
            _strip_wrap_nudges(messages)

    class _StreamedRound:
        """Transparent proxy over a streamed round's completed response,
        exposing the reasoning text accumulated from ``reasoning_*`` done
        events (``stream_reasoning``) — some OpenRouter hosts leave the
        completed item's content null, so the events are the durable source.
        Every other attribute delegates to the real response."""

        def __init__(self, response: Any, stream_reasoning: list[dict[str, Any]]):
            object.__setattr__(self, "_response", response)
            object.__setattr__(self, "stream_reasoning", stream_reasoning)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._response, name)

    async def _round(
        self, wire: list[Any], request_kwargs: dict[str, Any], *,
        on_stream_event: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        iteration: int = 0, dispatch_id: str | None = None,
    ) -> Any:
        """One LLM round of the tool loop (2026-10-03 trace uplift, Phase 2).

        Buffered by default (BOB_LLM_STREAMING gates the transport); when
        streaming is on, events are consumed and the round RETURNS
        ``event.response`` from ``response.completed``/``response.incomplete``
        — probe-verified on both rails (docs/llm-streaming-probe.md) to carry
        the full output items + usage, so every downstream consumer
        (function_call scan, citation rendering, item serialization, trace
        extraction) is identical between transports.

        Deltas are forwarded via ``on_stream_event(kind, data)`` — both
        reasoning dialects: OpenAI's ``reasoning_summary_*`` events and
        OpenRouter/GLM's raw ``reasoning_text`` events. Exceptions in the
        callback are swallowed (observability must never kill a turn)."""
        client = self._client_for(request_kwargs["model"])
        ls = getattr(self._get_settings(), "llm_streaming", None)
        streaming = bool(getattr(ls, "streaming_enabled", False)) if ls is not None else False

        if not streaming:
            return await client.responses.create(input=wire, **request_kwargs)

        async def _forward(kind: str, data: dict[str, Any]) -> None:
            if on_stream_event is None:
                return
            try:
                await on_stream_event(kind, {"iteration": iteration, **data})
            except Exception:
                logger.warning(
                    "on_stream_event callback failed: kind=%s dispatch_id=%s "
                    "iteration=%d", kind, dispatch_id, iteration, exc_info=True)

        # Reasoning ground truth from the event stream — some OpenRouter
        # hosts leave the completed response's reasoning item content null,
        # so the done-events are the fallback capture path (see
        # _extract_reasoning).
        stream_reasoning: list[dict[str, Any]] = []

        def _note_reasoning(text: str, raw: bool) -> None:
            text = (text or "").strip()
            if text:
                stream_reasoning.append(
                    {"source": "raw" if raw else "summary", "text": text})

        stream = await client.responses.create(
            input=wire, stream=True, **request_kwargs)
        async for event in stream:
            et = getattr(event, "type", "")
            if et == "response.output_text.delta":
                if event.delta:
                    await _forward("text_delta", {"text": event.delta})
            elif et == "response.reasoning_summary_text.delta":
                if event.delta:
                    await _forward("reasoning_delta", {"text": event.delta})
            elif et == "response.reasoning_summary_part.done":
                part = getattr(event, "part", None)
                part_text = getattr(part, "text", "") or ""
                _note_reasoning(part_text, raw=False)
                await _forward("reasoning_part", {"text": part_text, "raw": False})
            elif et == "response.reasoning_text.delta":
                if event.delta:
                    await _forward("reasoning_delta", {"text": event.delta, "raw": True})
            elif et == "response.reasoning_text.done":
                done_text = getattr(event, "text", "") or ""
                _note_reasoning(done_text, raw=True)
                await _forward("reasoning_part", {"text": done_text, "raw": True})
            elif et == "response.output_item.added":
                item = getattr(event, "item", None)
                if getattr(item, "type", None) == "function_call":
                    await _forward("tool_started", {
                        "name": getattr(item, "name", None),
                        "item_id": getattr(item, "id", None),
                        "call_id": getattr(item, "call_id", None)})
            elif et == "response.function_call_arguments.delta":
                if event.delta:
                    await _forward("tool_args", {
                        "item_id": event.item_id, "delta": event.delta})
            elif et in ("response.completed", "response.incomplete"):
                return self._StreamedRound(event.response, stream_reasoning)
            elif et in ("response.failed", "response.error", "error"):
                err = getattr(getattr(event, "response", None), "error", None)
                raise RuntimeError(f"LLM stream failed: {err or et}")
        raise RuntimeError("LLM stream ended without a terminal event")

    async def _forced_wrapup(
        self, messages: list[dict[str, Any]], resolved_model: str,
        request_kwargs: dict[str, Any], *, dispatch_id: str | None,
        session_key: str | None, fallback: str,
        base_len: int | None = None, history_keep: int = 20,
        send_tool_turn: bool = False,
        on_stream_event: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> str:
        """Exhaustion path: one final LLM round with tools stripped, so the
        model writes its own closing reply instead of hitting a canned stop.
        Falls back to the legacy canned string when the round fails or comes
        back empty — callers always get a non-empty string. ``base_len`` set
        means the loop was using a trimmed history view; the wrap-up round
        rides the same view. ``send_tool_turn`` selects the delivery-framed
        wrap text (the caller delivers the final text on the model's behalf)."""
        kwargs = {k: v for k, v in request_kwargs.items() if k != "tools"}
        messages.append({"role": "system", "content": (
            _SELF_WRAP_FINAL_SEND if send_tool_turn else _SELF_WRAP_FINAL)})
        try:
            wire = tool_loop_folding.iteration_view(
                messages, base_len, history_keep=history_keep) \
                if base_len is not None else messages
            wire = _video_safe_wire(
                wire, dispatch_id=dispatch_id, model=resolved_model)
            if on_stream_event is not None:
                try:
                    await on_stream_event("round_started", {"iteration": -1})
                except Exception:
                    pass
            response = await self._round(
                wire, kwargs, on_stream_event=on_stream_event,
                iteration=-1, dispatch_id=dispatch_id)
        except Exception:
            logger.error(
                "OpenAI forced wrap-up round failed: model=%s dispatch_id=%s "
                "session_key=%s", resolved_model, dispatch_id, session_key,
                exc_info=True)
            return fallback
        content = _response_text_with_citations(response)
        if not content:
            logger.warning(
                "OpenAI forced wrap-up round empty: model=%s dispatch_id=%s "
                "session_key=%s", resolved_model, dispatch_id, session_key)
        return content or fallback

    async def quick_prompt(self, prompt: str) -> str:
        """Send a bare prompt string and return the response."""
        return await self.prompt(
            messages=[{"role": "user", "content": prompt}],
        )

    @staticmethod
    def _extract_cached_tokens(usage: Any) -> int | None:
        if not usage:
            return None
        details = getattr(usage, "input_tokens_details", None)
        if details and hasattr(details, "cached_tokens"):
            return details.cached_tokens
        return None


def _tool_result_messages(
    result: str | ImageInjection | VideoInjection,
    call_id: str,
    *,
    video_supported: bool,
) -> list[dict[str, Any]]:
    """Messages to append for a tool result that may carry media.

    Plain-string results become the function_call_output row only.
    ImageInjection rides function_call_output.output as typed parts
    (probe-verified on OpenAI-direct and OpenRouter/GLM, 2026-09-30).
    VideoInjection rides a synthetic USER message with an input_video part
    when the serving model supports native video (video_url in user content
    is the rail-verified shape, 2026-09-05) — video must NOT ride
    function_call_output: OpenRouter's Responses validator accepts
    input_text/input_image parts there but rejects input_video, which 400s
    the whole request and, once stored, poisons every later turn in the
    session (2026-10-02 incident, six invalid_prompt errors in one day).
    Unsupported models degrade to the video's first frame (or a text-only
    note when no frame can be extracted), so a modal mismatch never crashes
    the turn. Empty data_urls (error returns from read_image/read_video)
    append the text row only.
    """
    if not isinstance(result, (ImageInjection, VideoInjection)):
        return [{
            "type": "function_call_output",
            "call_id": call_id,
            "output": result,
        }]

    text = result.text
    part: dict[str, Any] | None = None
    if isinstance(result, VideoInjection):
        if video_supported and result.data_url:
            return [
                {"type": "function_call_output", "call_id": call_id,
                 "output": text + " (video attached in the following message)"},
                {"role": "user", "content": [
                    {"type": "input_text",
                     "text": f"[video tool output for {call_id}]"},
                    {"type": "input_video", "video_url": result.data_url},
                ]},
            ]
        else:
            # Degrade: the serving model can't watch video — show its first
            # frame instead, when the source path is available.
            frame_url = ""
            if result.path:
                from server.services.prompt_assembler import _extract_video_frame
                frame_path = _extract_video_frame(result.path)
                if frame_path:
                    try:
                        frame_url = "data:image/jpeg;base64," + base64.b64encode(
                            Path(frame_path).read_bytes()).decode()
                    except OSError:
                        frame_url = ""
            if frame_url:
                text = result.text + " (this model lacks native video input — first frame shown)"
                part = {"type": "input_image", "image_url": frame_url}
            else:
                return [{
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": (result.text + " — this model cannot watch video; extract "
                               "frames yourself (ffmpeg) and view them with read_image"),
                }]
    elif result.data_url:
        part = {"type": "input_image", "image_url": result.data_url}

    # Images ride the tool output itself (2026-09-30, probe-verified on
    # OpenAI-direct AND OpenRouter/GLM: function_call_output.output accepts
    # typed parts). The old synthetic user block persisted ~800KB data URLs
    # that no mechanism folded — goal-room inspection turns ballooned to
    # 1.2M tokens. On this rail, tool-loop folding owns the aging.
    output: Any = text
    if part is not None:
        output = [{"type": "input_text", "text": text}, part]
    rows: list[dict[str, Any]] = [{
        "type": "function_call_output",
        "call_id": call_id,
        "output": output,
    }]
    return rows


def strip_unsupported_fco_video(items: list[Any]) -> tuple[list[Any], int]:
    """Wire-shape guard for function_call_output parts arrays: wrap bare
    string parts as input_text, and remove input_video parts.

    OpenRouter's Responses validator rejects input_video in tool outputs
    (input_text/input_image are fine) with a 400 invalid_prompt whose error
    body is tens of KB of raw Zod JSON. Rows produced before 2026-10-03 put
    video there, and stored tool_blocks_json replays those rows verbatim —
    so this runs on every request wire as the last line of defence,
    de-poisoning legacy history. User-message input_video parts are the
    supported shape and pass through untouched.

    Returns (items, number of FCO rows stripped) — the ORIGINAL list object
    when nothing needed stripping (callers pass the same list on every
    iteration; identity preservation keeps wire-identity tests honest).
    """
    out: list[Any] = []
    stripped = 0
    for item in items:
        # Bare strings inside a parts array are equally invalid (the
        # 2026-09-30 image-elision stub 400'd every image-heavy frigate
        # turn): wrap them as typed input_text parts.
        if (
            isinstance(item, dict)
            and item.get("type") == "function_call_output"
            and isinstance(item.get("output"), list)
            and any(isinstance(p, str) for p in item["output"])
        ):
            stripped += 1
            item = dict(item)
            item["output"] = [
                {"type": "input_text", "text": p} if isinstance(p, str) else p
                for p in item["output"]]
        if (
            isinstance(item, dict)
            and item.get("type") == "function_call_output"
            and isinstance(item.get("output"), list)
            and any(
                isinstance(p, dict) and p.get("type") == "input_video"
                for p in item["output"]
            )
        ):
            stripped += 1
            kept = [
                p for p in item["output"]
                if not (isinstance(p, dict) and p.get("type") == "input_video")
            ]
            texts = [
                p.get("text", "")
                for p in kept
                if isinstance(p, dict) and p.get("type") == "input_text"
            ]
            note = (" [video part removed — this rail rejects input_video "
                    "in tool outputs; fetch the clip with read_video if "
                    "needed]")
            clean = dict(item)
            clean["output"] = " ".join(t for t in texts if t).strip() + note
            out.append(clean)
        else:
            out.append(item)
    if not stripped:
        return items, 0
    return out, stripped


def _video_safe_wire(items: list[Any], *, dispatch_id: str | None,
                     model: str) -> list[Any]:
    """Apply strip_unsupported_fco_video with a log line when it fires."""
    wire, stripped = strip_unsupported_fco_video(items)
    if stripped:
        logger.warning(
            "repaired %d function_call_output row(s) on the request wire "
            "(bare-string or input_video parts) model=%s dispatch_id=%s",
            stripped, model, dispatch_id)
    return wire


def _raise_openai_error(exc: Exception) -> NoReturn:
    """Re-raise OpenAI SDK errors with context."""
    if _openai_module is not None:
        from openai import APIStatusError, APITimeoutError

        if isinstance(exc, (APIStatusError, APITimeoutError)):
            raise RuntimeError(f"OpenAI API error: {exc}") from exc
    raise RuntimeError(f"OpenAI call failed: {exc}") from exc
