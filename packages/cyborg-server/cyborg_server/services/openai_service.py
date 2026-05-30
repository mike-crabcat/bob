"""OpenAI LLM service using the Responses API."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Awaitable
from dataclasses import dataclass
from typing import Any, NoReturn

from cyborg_server.context import AppContext
from cyborg_server.services.base import BaseService
from cyborg_server.services.tools import ImageInjection

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
        raise RuntimeError("openai SDK is not installed. Install with: pip install cyborg-server[openai]")
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
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
            raise RuntimeError("OpenAI is not configured. Set CYBORG_OPENAI_API_KEY.")
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
            content = response.output_text or ""
            usage = getattr(response, "usage", None)

            cached_tokens = self._extract_cached_tokens(usage)

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
    ) -> str:
        """Multi-turn chat with tool calling via Responses API.

        Loops: send input → check for function_call items → execute → feed back.
        Returns the final text response.
        """
        resolved_model = model or self._get_settings().openai.default_model
        merged_tools = self._merge_tools(tools)
        t0 = time.monotonic()

        for iteration in range(max_iterations):
            response = await self.client.responses.create(
                model=resolved_model,
                input=messages,
                tools=merged_tools,
            )

            # Check for function calls in output
            function_calls = [
                item for item in response.output
                if getattr(item, "type", None) == "function_call"
            ]

            if not function_calls:
                elapsed = time.monotonic() - t0
                content = response.output_text or ""
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
                usage = getattr(response, "usage", None)
                cached_tokens = self._extract_cached_tokens(usage)
                logger.info(
                    "OpenAI tool call finished: model=%s iterations=%d latency=%.2fs "
                    "tool_calls_in_turn=%d tokens=%s cached_tokens=%s",
                    resolved_model, iteration + 1, elapsed,
                    iteration,
                    usage.total_tokens if usage else None,
                    cached_tokens,
                )
                if stream_result is not None:
                    stream_result.prompt_tokens = usage.input_tokens if usage else None
                    stream_result.completion_tokens = usage.output_tokens if usage else None
                    stream_result.total_tokens = usage.total_tokens if usage else None
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
                else:
                    try:
                        tool_args = json.loads(fc.arguments)
                        result = await handler(**tool_args)
                    except Exception as e:
                        result = f"Error: {e}"
                        logger.warning("Tool %s failed: %s", fc.name, e)

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
    ) -> AsyncIterator[str]:
        """Stream chat with tool calling. Runs tool calls non-streamingly,
        then streams the final text response for real-time consumption."""
        resolved_model = model or self._get_settings().openai.default_model
        merged_tools = self._merge_tools(tools)

        # Tool loop: non-streaming rounds until LLM gives a text response
        for iteration in range(max_iterations):
            response = await self.client.responses.create(
                model=resolved_model,
                input=messages,
                tools=merged_tools,
            )

            function_calls = [
                item for item in response.output
                if getattr(item, "type", None) == "function_call"
            ]

            if not function_calls:
                # No tool calls — stream the final text response
                content = response.output_text or ""
                if content:
                    yield content
                return

            # Append output items and execute tool calls
            messages.extend(_output_items_to_dicts(response.output))
            for fc in function_calls:
                handler = tool_handlers.get(fc.name)
                tool_args: dict = {}
                if handler is None:
                    result = f"Error: unknown tool '{fc.name}'"
                else:
                    try:
                        tool_args = json.loads(fc.arguments)
                        result = await handler(**tool_args)
                    except Exception as e:
                        result = f"Error: {e}"
                        logger.warning("Tool %s failed: %s", fc.name, e)

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

        # Hit max iterations — make one final streaming call
        logger.warning("chat_stream_with_tools hit max iterations: %d", max_iterations)
        async for chunk in self.chat_stream(
            messages=messages,
            model=resolved_model,
        ):
            if chunk:
                yield chunk

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
