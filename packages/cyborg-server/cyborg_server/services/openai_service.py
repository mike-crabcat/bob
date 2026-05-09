"""Direct OpenAI LLM service for evaluation and comparison."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Awaitable
from dataclasses import dataclass, field
from typing import Any, NoReturn

from cyborg_server.context import AppContext
from cyborg_server.services.base import BaseService

try:
    from openai import AsyncOpenAI
    import openai as _openai_module
except ImportError:
    AsyncOpenAI = None  # type: ignore[assignment, misc]
    _openai_module = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4.1-mini"


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


class OpenAIService(BaseService):
    """LLM reasoning through direct OpenAI API calls."""

    @property
    def client(self) -> Any:
        settings = self._get_settings().openai
        if not settings.enabled:
            raise RuntimeError("OpenAI is not configured. Set CYBORG_OPENAI_API_KEY.")
        return _get_cached_client(settings.api_key, settings.base_url)

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> str:
        """Non-streaming chat completion."""
        resolved_model = model or self._get_settings().openai.default_model
        kwargs: dict[str, Any] = {
            "model": resolved_model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        t0 = time.monotonic()
        try:
            response = await self.client.chat.completions.create(**kwargs)
            elapsed = time.monotonic() - t0
            usage = getattr(response, "usage", None)
            content = response.choices[0].message.content

            # Extract cached tokens from usage details
            cached_tokens = None
            if usage and hasattr(usage, "prompt_tokens_details"):
                details = usage.prompt_tokens_details
                if details and hasattr(details, "cached_tokens"):
                    cached_tokens = details.cached_tokens

            logger.info(
                "OpenAI chat: model=%s latency=%.2fs stop=%s "
                "prompt_tokens=%s completion_tokens=%s total_tokens=%s "
                "cached_tokens=%s "
                "input_chars=%d output_chars=%d",
                resolved_model,
                elapsed,
                response.choices[0].finish_reason,
                usage.prompt_tokens if usage else None,
                usage.completion_tokens if usage else None,
                usage.total_tokens if usage else None,
                cached_tokens,
                sum(len(m.get("content", "")) for m in messages),
                len(content or ""),
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
        """Streaming chat completion, yielding content deltas."""
        resolved_model = model or self._get_settings().openai.default_model
        kwargs: dict[str, Any] = {
            "model": resolved_model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        t0 = time.monotonic()
        first_token_time: float | None = None
        chunk_count = 0
        total_chars = 0
        finish_reason = None
        final_usage = None

        try:
            response = await self.client.chat.completions.create(**kwargs)
            async for chunk in response:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                content = getattr(delta, "content", None)
                if content:
                    if first_token_time is None:
                        first_token_time = time.monotonic()
                    chunk_count += 1
                    total_chars += len(content)
                    yield content
                if chunk.choices[0].finish_reason is not None:
                    finish_reason = chunk.choices[0].finish_reason
                if hasattr(chunk, "usage") and chunk.usage:
                    final_usage = chunk.usage
        except Exception as exc:
            logger.error("OpenAI streaming error: %s", exc)
            _raise_openai_error(exc)

        elapsed = time.monotonic() - t0
        # Extract cached tokens from streaming usage
        cached_tokens = None
        if final_usage and hasattr(final_usage, "prompt_tokens_details"):
            details = final_usage.prompt_tokens_details
            if details and hasattr(details, "cached_tokens"):
                cached_tokens = details.cached_tokens
        logger.info(
            "OpenAI stream: model=%s latency=%.2fs ttft=%.2fs "
            "chunks=%d output_chars=%d stop=%s "
            "prompt_tokens=%s completion_tokens=%s total_tokens=%s "
            "cached_tokens=%s",
            resolved_model,
            elapsed,
            (first_token_time - t0) if first_token_time else elapsed,
            chunk_count,
            total_chars,
            finish_reason,
            final_usage.prompt_tokens if final_usage else None,
            final_usage.completion_tokens if final_usage else None,
            final_usage.total_tokens if final_usage else None,
            cached_tokens,
        )

        # Populate caller's result object
        if stream_result is not None:
            stream_result.prompt_tokens = final_usage.prompt_tokens if final_usage else None
            stream_result.completion_tokens = final_usage.completion_tokens if final_usage else None
            stream_result.total_tokens = final_usage.total_tokens if final_usage else None
            stream_result.cached_tokens = cached_tokens
            stream_result.latency_seconds = elapsed
            stream_result.ttft_seconds = (first_token_time - t0) if first_token_time else None
            stream_result.finish_reason = finish_reason

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_handlers: dict[str, Callable[..., Awaitable[str]]],
        *,
        model: str | None = None,
        max_iterations: int = 10,
        stream_result: StreamResult | None = None,
    ) -> str:
        """Multi-turn chat with tool calling.

        Loops: send messages → check for tool_calls → execute → feed back.
        Returns the final text response.
        """
        import json as _json

        resolved_model = model or self._get_settings().openai.default_model
        t0 = time.monotonic()

        for iteration in range(max_iterations):
            response = await self.client.chat.completions.create(
                model=resolved_model,
                messages=messages,
                tools=tools,
            )
            message = response.choices[0].message

            if not message.tool_calls:
                elapsed = time.monotonic() - t0
                usage = getattr(response, "usage", None)
                logger.info(
                    "OpenAI tool call finished: model=%s iterations=%d latency=%.2fs "
                    "tool_calls=%d tokens=%s",
                    resolved_model, iteration + 1, elapsed,
                    iteration,
                    usage.total_tokens if usage else None,
                )
                if stream_result is not None:
                    stream_result.prompt_tokens = usage.prompt_tokens if usage else None
                    stream_result.completion_tokens = usage.completion_tokens if usage else None
                    stream_result.total_tokens = usage.total_tokens if usage else None
                    stream_result.latency_seconds = elapsed
                    stream_result.finish_reason = response.choices[0].finish_reason
                return message.content or ""

            # Execute tool calls
            messages.append({
                "role": message.role,
                "content": message.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in message.tool_calls
                ],
            })

            for tc in message.tool_calls:
                handler = tool_handlers.get(tc.function.name)
                if handler is None:
                    result = f"Error: unknown tool '{tc.function.name}'"
                else:
                    try:
                        args = json.loads(tc.function.arguments)
                        result = await handler(**args)
                    except Exception as e:
                        result = f"Error: {e}"
                        logger.warning("Tool %s failed: %s", tc.function.name, e)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

        elapsed = time.monotonic() - t0
        logger.warning("OpenAI tool call hit max iterations: model=%s max=%d", resolved_model, max_iterations)
        return "Max tool call iterations reached."

    async def quick_prompt(self, prompt: str) -> str:
        """Send a bare prompt string and return the response."""
        return await self.chat(
            messages=[{"role": "user", "content": prompt}],
        )


def _raise_openai_error(exc: Exception) -> NoReturn:
    """Re-raise OpenAI SDK errors with context."""
    if _openai_module is not None:
        from openai import APIStatusError, APITimeoutError

        if isinstance(exc, (APIStatusError, APITimeoutError)):
            raise RuntimeError(f"OpenAI API error: {exc}") from exc
    raise RuntimeError(f"OpenAI call failed: {exc}") from exc
