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

from bob_server.context import AppContext
from bob_server.services.base import BaseService
from bob_server.services.tools import ImageInjection

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
            result.append({
                "type": "message",
                "role": item.role,
                "content": [{"type": c.type, "text": c.text} for c in item.content] if item.content else [],
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
_cached_client: tuple[str, str, Any] | None = None  # (api_key, base_url, client)


def _get_cached_client(api_key: str, base_url: str) -> Any:
    global _cached_client
    if _cached_client is not None and _cached_client[0] == api_key and _cached_client[1] == base_url:
        return _cached_client[2]
    if AsyncOpenAI is None:
        raise RuntimeError("openai SDK is not installed. Install with: pip install bob-server[openai]")
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=Timeout(300.0, connect=30.0),
    )
    _cached_client = (api_key, base_url, client)
    logger.info("OpenAI client created for base_url=%s", base_url)
    return client


def _model_skips_temperature(model: str) -> bool:
    """Return True for models that don't accept the temperature parameter."""
    return any(model.startswith(p) for p in ("gpt-5.5", "o1", "o3", "o4"))


class OpenAIService(BaseService):
    """LLM reasoning through OpenAI Responses API."""

    @property
    def client(self) -> Any:
        settings = self._get_settings().openai
        if not settings.enabled:
            raise RuntimeError("OpenAI is not configured. Set BOB_OPENAI_API_KEY.")
        return _get_cached_client(settings.api_key, settings.base_url)

    @property
    def _web_search_tool(self) -> dict[str, Any] | None:
        if self._get_settings().openai.web_search_enabled:
            return {"type": "web_search", "search_context_size": "medium"}
        return None

    def _merge_tools(self, tools: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """Merge caller-provided tools with built-in tools like web_search."""
        merged: list[dict[str, Any]] = []
        if self._web_search_tool:
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

        tools = self._merge_tools()
        if tools:
            kwargs["tools"] = tools

        t0 = time.monotonic()
        try:
            response = await self.client.responses.create(**kwargs)
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

        tools = self._merge_tools()
        if tools:
            kwargs["tools"] = tools

        t0 = time.monotonic()
        first_token_time: float | None = None
        chunk_count = 0
        total_chars = 0
        final_usage = None
        response_id = None

        try:
            response = await self.client.responses.create(**kwargs)
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
        stream_result: StreamResult | None = None,
        on_tool_call: Callable[[str, dict, str], Awaitable[None]] | None = None,
        on_iteration_complete: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
        dispatch_id: str | None = None,
        session_key: str | None = None,
        log_id: str | None = None,
    ) -> str:
        """Multi-turn chat with tool calling via Responses API.

        Loops: send input → check for function_call items → execute → feed back.
        Returns the final text response.
        """
        resolved_model = model or self._get_settings().openai.default_model
        merged_tools = self._merge_tools(tools)
        t0 = time.monotonic()

        total_input = total_output = total_total = 0
        total_cached = 0

        for iteration in range(max_iterations):
            response = await self.client.responses.create(
                model=resolved_model,
                input=messages,
                tools=merged_tools,
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
                    logger.warning(
                        "OpenAI empty response: model=%s status=%s output_types=%s refusal=%s",
                        resolved_model,
                        getattr(response, "status", None),
                        [getattr(item, "type", None) for item in (response.output or [])],
                        getattr(response, "refusal", None) or next(
                            (getattr(item, "refusal", None) for item in (response.output or [])
                             if getattr(item, "type", None) == "message"), None
                        ),
                    )
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

        elapsed = time.monotonic() - t0
        logger.warning("OpenAI tool call hit max iterations: model=%s max=%d", resolved_model, max_iterations)
        return "Max tool call iterations reached."

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
        merged_tools = self._merge_tools(tools)
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
            response = await self.client.responses.create(
                model=resolved_model,
                input=messages,
                tools=merged_tools,
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
