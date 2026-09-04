"""OpenAI LLM service using the Responses API."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import AsyncIterator, Callable, Awaitable
from httpx import Timeout
from dataclasses import dataclass
from typing import Any, NoReturn

from server.context import AppContext
from server.services import model_registry
from server.services.base import BaseService
from server.services.tools import ImageInjection

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
    """Convert Responses API output items to plain dicts for JSON serialization."""
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
# Both are stripped from the messages list before chat_with_tools returns so
# they never persist into conversation history.
_SELF_WRAP_NUDGE = (
    "You are close to this turn's budget (time or tool calls). Stop starting "
    "new work: finish the current step, then reply to the user now with what "
    "you have, noting anything left unfinished.")
_SELF_WRAP_FINAL = (
    "This turn's budget is exhausted. Do not call any more tools. Reply to "
    "the user right now with a short summary of what you found or did, and "
    "say plainly what is left undone.")
_LEGACY_TIME_STOP = (
    "Stopped at the turn's wall-clock budget — work done so far is complete, "
    "remaining steps were skipped.")
_LEGACY_ITER_STOP = "Max tool call iterations reached."


def _strip_wrap_nudges(messages: list[dict[str, Any]]) -> None:
    """Remove injected budget nudges in place (turn-scoped, never persisted)."""
    messages[:] = [
        m for m in messages
        if not (isinstance(m, dict)
                and m.get("content") in (_SELF_WRAP_NUDGE, _SELF_WRAP_FINAL))
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

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        stream_result: StreamResult | None = None,
    ) -> str:
        """Non-streaming chat completion via Responses API."""
        resolved_model = model or self._get_settings().openai.default_model
        kwargs: dict[str, Any] = {
            "model": resolved_model,
            "input": messages,
        }
        if not _model_skips_temperature(resolved_model):
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_output_tokens"] = max_tokens
        effort = _effective_reasoning_effort(
            resolved_model, reasoning_effort, self._get_settings())
        if effort is not None:
            kwargs["reasoning"] = {"effort": effort}

        tools = self._merge_tools(model=resolved_model)
        if tools:
            kwargs["tools"] = tools

        t0 = time.monotonic()
        try:
            response = await self._client_for(resolved_model).responses.create(**kwargs)
            elapsed = time.monotonic() - t0
            content = _response_text_with_citations(response)
            usage = getattr(response, "usage", None)

            cached_tokens = self._extract_cached_tokens(usage)

            if stream_result is not None:
                stream_result.prompt_tokens = usage.input_tokens if usage else None
                stream_result.completion_tokens = usage.output_tokens if usage else None
                stream_result.total_tokens = usage.total_tokens if usage else None
                stream_result.cached_tokens = cached_tokens
                stream_result.latency_seconds = elapsed

            logger.info(
                "OpenAI chat: model=%s latency=%.2fs "
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
            logger.error("OpenAI chat failed: model=%s latency=%.2fs error=%s", resolved_model, elapsed, e)
            _raise_openai_error(e)

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        stream_result: StreamResult | None = None,
    ) -> AsyncIterator[str]:
        """Streaming chat completion via Responses API, yielding text deltas."""
        resolved_model = model or self._get_settings().openai.default_model
        kwargs: dict[str, Any] = {
            "model": resolved_model,
            "input": messages,
            "stream": True,
        }
        if not _model_skips_temperature(resolved_model):
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_output_tokens"] = max_tokens
        effort = _effective_reasoning_effort(
            resolved_model, None, self._get_settings())
        if effort is not None:
            kwargs["reasoning"] = {"effort": effort}

        tools = self._merge_tools(model=resolved_model)
        if tools:
            kwargs["tools"] = tools

        t0 = time.monotonic()
        first_token_time: float | None = None
        chunk_count = 0
        total_chars = 0
        final_usage = None
        response_id = None

        try:
            response = await self._client_for(resolved_model).responses.create(**kwargs)
            async for event in response:
                if event.type == "response.output_text.delta":
                    delta = event.delta
                    if delta:
                        if first_token_time is None:
                            first_token_time = time.monotonic()
                        chunk_count += 1
                        total_chars += len(delta)
                        yield delta
                elif event.type == "response.completed":
                    final_usage = getattr(event.response, "usage", None)
                    response_id = getattr(event.response, "id", None)
        except Exception as exc:
            logger.error("OpenAI streaming error: %s", exc)
            _raise_openai_error(exc)

        elapsed = time.monotonic() - t0
        cached_tokens = self._extract_cached_tokens(final_usage)
        logger.info(
            "OpenAI stream: model=%s latency=%.2fs ttft=%.2fs "
            "chunks=%d output_chars=%s response_id=%s "
            "input_tokens=%s output_tokens=%s total_tokens=%s "
            "cached_tokens=%s",
            resolved_model, elapsed,
            (first_token_time - t0) if first_token_time else elapsed,
            chunk_count, total_chars, response_id,
            final_usage.input_tokens if final_usage else None,
            final_usage.output_tokens if final_usage else None,
            final_usage.total_tokens if final_usage else None,
            cached_tokens,
        )

        if stream_result is not None:
            stream_result.prompt_tokens = final_usage.input_tokens if final_usage else None
            stream_result.completion_tokens = final_usage.output_tokens if final_usage else None
            stream_result.total_tokens = final_usage.total_tokens if final_usage else None
            stream_result.cached_tokens = cached_tokens
            stream_result.latency_seconds = elapsed
            stream_result.ttft_seconds = (first_token_time - t0) if first_token_time else None

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_handlers: dict[str, Callable[..., Awaitable[str | ImageInjection]]],
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
    ) -> str:
        """Multi-turn chat with tool calling via Responses API.

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
        """
        resolved_model = model or self._get_settings().openai.default_model
        merged_tools = self._merge_tools(tools, model=resolved_model)
        request_kwargs: dict[str, Any] = {
            "model": resolved_model,
            "tools": merged_tools,
        }
        effort = _effective_reasoning_effort(
            resolved_model, None, self._get_settings())
        if effort is not None:
            request_kwargs["reasoning"] = {"effort": effort}
        t0 = time.monotonic()
        deadline = t0 + time_limit_seconds if time_limit_seconds is not None else None

        wrap = self._get_settings().self_wrap
        nudge_at = (t0 + time_limit_seconds * wrap.duration_fraction
                    if time_limit_seconds is not None else None)
        nudged = False

        total_input = total_output = total_total = 0
        total_cached = 0

        try:
            for iteration in range(max_iterations):
                now = time.monotonic()
                if (wrap.enabled and not nudged
                        and ((nudge_at is not None and now >= nudge_at)
                             or iteration >= max_iterations - wrap.iteration_margin)):
                    messages.append({"role": "system", "content": _SELF_WRAP_NUDGE})
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
                    if not wrap.enabled:
                        return _LEGACY_TIME_STOP
                    return await self._forced_wrapup(
                        messages, resolved_model, request_kwargs,
                        dispatch_id=dispatch_id, session_key=session_key,
                        fallback=_LEGACY_TIME_STOP)

                response = await self._client_for(resolved_model).responses.create(
                    input=messages,
                    **request_kwargs,
                )

                usage = getattr(response, "usage", None)
                if usage:
                    total_input  += usage.input_tokens or 0
                    total_output += usage.output_tokens or 0
                    total_total  += usage.total_tokens or 0
                    total_cached += self._extract_cached_tokens(usage) or 0

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
                                            if isinstance(hc_result, ImageInjection)
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
                    if stream_result is not None:
                        stream_result.prompt_tokens = total_input
                        stream_result.completion_tokens = total_output
                        stream_result.total_tokens = total_total
                        stream_result.cached_tokens = total_cached
                        stream_result.latency_seconds = elapsed
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

                    if isinstance(result, ImageInjection):
                        messages.append({
                            "type": "function_call_output",
                            "call_id": fc.call_id,
                            "output": result.text,
                        })
                        messages.append({
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": result.text},
                                {"type": "input_image", "image_url": result.data_url},
                            ],
                        })
                    else:
                        messages.append({
                            "type": "function_call_output",
                            "call_id": fc.call_id,
                            "output": result,
                        })

                    if on_tool_call:
                        try:
                            summary = result.text[:200] if isinstance(result, ImageInjection) else result[:200]
                            await on_tool_call(fc.name, tool_args, summary)
                        except Exception:
                            pass

                logger.info(
                    "chat_with_tools: iteration=%d function_calls=%d",
                    iteration + 1, len(function_calls),
                )

                if on_iteration_complete:
                    try:
                        await on_iteration_complete(messages)
                    except Exception:
                        pass

            logger.warning(
                "OpenAI tool call hit max iterations: model=%s max=%d",
                resolved_model, max_iterations)
            if not wrap.enabled:
                return _LEGACY_ITER_STOP
            return await self._forced_wrapup(
                messages, resolved_model, request_kwargs,
                dispatch_id=dispatch_id, session_key=session_key,
                fallback=_LEGACY_ITER_STOP)
        finally:
            # Budget nudges are turn-scoped guidance — never persist them
            # into the conversation history the caller keeps.
            _strip_wrap_nudges(messages)

    async def _forced_wrapup(
        self, messages: list[dict[str, Any]], resolved_model: str,
        request_kwargs: dict[str, Any], *, dispatch_id: str | None,
        session_key: str | None, fallback: str,
    ) -> str:
        """Exhaustion path: one final LLM round with tools stripped, so the
        model writes its own closing reply instead of hitting a canned stop.
        Falls back to the legacy canned string when the round fails or comes
        back empty — callers always get a non-empty string."""
        kwargs = {k: v for k, v in request_kwargs.items() if k != "tools"}
        messages.append({"role": "system", "content": _SELF_WRAP_FINAL})
        try:
            response = await self._client_for(resolved_model).responses.create(
                input=messages, **kwargs)
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

    async def chat_stream_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_handlers: dict[str, Callable[..., Awaitable[str | ImageInjection]]],
        *,
        model: str | None = None,
        max_iterations: int = 100,
        on_tool_call: Callable[[str, dict, str], Awaitable[None]] | None = None,
        on_iteration_complete: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
        dispatch_id: str | None = None,
        session_key: str | None = None,
        log_id: str | None = None,
        stream_result: StreamResult | None = None,
    ) -> AsyncIterator[str]:
        """Stream chat with tool calling. Runs tool calls non-streamingly,
        then streams the final text response for real-time consumption."""
        resolved_model = model or self._get_settings().openai.default_model
        merged_tools = self._merge_tools(tools, model=resolved_model)
        request_kwargs: dict[str, Any] = {
            "model": resolved_model,
            "tools": merged_tools,
        }
        effort = _effective_reasoning_effort(
            resolved_model, None, self._get_settings())
        if effort is not None:
            request_kwargs["reasoning"] = {"effort": effort}
        t0 = time.monotonic()

        total_input = total_output = total_total = 0
        total_cached = 0

        def _flush_stream_result() -> None:
            if stream_result is None:
                return
            stream_result.prompt_tokens = total_input
            stream_result.completion_tokens = total_output
            stream_result.total_tokens = total_total
            stream_result.cached_tokens = total_cached
            stream_result.latency_seconds = time.monotonic() - t0

        # Tool loop: non-streaming rounds until LLM gives a text response
        for iteration in range(max_iterations):
            response = await self._client_for(resolved_model).responses.create(
                input=messages,
                **request_kwargs,
            )

            usage = getattr(response, "usage", None)
            if usage:
                total_input  += usage.input_tokens or 0
                total_output += usage.output_tokens or 0
                total_total  += usage.total_tokens or 0
                total_cached += self._extract_cached_tokens(usage) or 0

            function_calls = [
                item for item in response.output
                if getattr(item, "type", None) == "function_call"
            ]

            if not function_calls:
                # No tool calls — stream the final text response
                content = response.output_text or ""
                # Recover Hermes-style <tool_call> XML before streaming out.
                hermes_calls = _parse_hermes_tool_calls(content)
                if hermes_calls:
                    for hc_name, hc_args in hermes_calls:
                        handler = tool_handlers.get(hc_name)
                        if handler is None:
                            logger.warning(
                                "Hermes tool call (stream) referenced unknown tool: "
                                "tool=%s dispatch_id=%s session_key=%s",
                                hc_name, dispatch_id, session_key,
                            )
                            continue
                        try:
                            await handler(**hc_args)
                        except Exception as hc_exc:
                            logger.error(
                                "Hermes tool call (stream) failed: tool=%s "
                                "dispatch_id=%s session_key=%s error=%s",
                                hc_name, dispatch_id, session_key, hc_exc,
                                exc_info=True,
                            )
                    content = _strip_hermes_tool_calls(content)
                if content:
                    yield content
                _flush_stream_result()
                return

            # Append output items and execute tool calls
            messages.extend(_output_items_to_dicts(response.output))
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

                messages.append({
                    "type": "function_call_output",
                    "call_id": fc.call_id,
                    "output": result,
                })

                if on_tool_call:
                    try:
                        summary = result.text[:200] if isinstance(result, ImageInjection) else result[:200]
                        await on_tool_call(fc.name, tool_args, summary)
                    except Exception:
                        pass

            logger.info(
                "chat_stream_with_tools: iteration=%d function_calls=%d",
                iteration + 1, len(function_calls),
            )

            if on_iteration_complete:
                try:
                    await on_iteration_complete(messages)
                except Exception:
                    pass

        # Hit max iterations — make one final streaming call.
        # Pass a child StreamResult so we can merge its usage into the running totals
        # rather than overwriting the intermediate iterations.
        logger.warning("chat_stream_with_tools hit max iterations: %d", max_iterations)
        fallback_result = StreamResult()
        async for chunk in self.chat_stream(
            messages=messages,
            model=resolved_model,
            stream_result=fallback_result,
        ):
            if chunk:
                yield chunk
        total_input  += fallback_result.prompt_tokens or 0
        total_output += fallback_result.completion_tokens or 0
        total_total  += fallback_result.total_tokens or 0
        total_cached += fallback_result.cached_tokens or 0
        _flush_stream_result()

    async def quick_prompt(self, prompt: str) -> str:
        """Send a bare prompt string and return the response."""
        return await self.chat(
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


def _raise_openai_error(exc: Exception) -> NoReturn:
    """Re-raise OpenAI SDK errors with context."""
    if _openai_module is not None:
        from openai import APIStatusError, APITimeoutError

        if isinstance(exc, (APIStatusError, APITimeoutError)):
            raise RuntimeError(f"OpenAI API error: {exc}") from exc
    raise RuntimeError(f"OpenAI call failed: {exc}") from exc
