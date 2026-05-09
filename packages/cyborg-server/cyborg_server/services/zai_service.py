"""Direct Z.ai LLM service for evaluation."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any, NoReturn

from cyborg_server.context import AppContext
from cyborg_server.services.base import BaseService

try:
    from zai import ZaiClient
    import zai as _zai_module
except ImportError:
    ZaiClient = None  # type: ignore[assignment, misc]
    _zai_module = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "glm-5.1"

# Module-level client cache so httpx reuses TCP connections across requests.
_cached_client: tuple[str, str, Any] | None = None  # (api_key, base_url, client)


def _get_cached_client(api_key: str, base_url: str) -> Any:
    global _cached_client
    if _cached_client is not None and _cached_client[0] == api_key and _cached_client[1] == base_url:
        return _cached_client[2]
    if ZaiClient is None:
        raise RuntimeError("zai-sdk is not installed. Install with: pip install cyborg-server[zai]")
    client = ZaiClient(api_key=api_key, base_url=base_url)
    _cached_client = (api_key, base_url, client)
    logger.info("Z.ai client created for base_url=%s", base_url)
    return client


class ZaiService(BaseService):
    """LLM reasoning through direct Z.ai API calls."""

    @property
    def client(self) -> Any:
        settings = self._get_settings().zai
        if not settings.enabled:
            raise RuntimeError("Z.ai is not configured. Set CYBORG_ZAI_API_KEY.")
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
        resolved_model = model or self._get_settings().zai.default_model
        kwargs: dict[str, Any] = {
            "model": resolved_model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        t0 = time.monotonic()
        try:
            response = await asyncio.to_thread(
                self.client.chat.completions.create, **kwargs
            )
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
                "Z.ai chat: model=%s latency=%.2fs stop=%s "
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
            logger.error("Z.ai chat failed: model=%s latency=%.2fs error=%s", resolved_model, elapsed, e)
            _raise_zai_error(e)

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """Streaming chat completion, yielding content deltas."""
        resolved_model = model or self._get_settings().zai.default_model
        kwargs: dict[str, Any] = {
            "model": resolved_model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        t0 = time.monotonic()
        first_token_time: float | None = None
        chunk_count = 0
        total_chars = 0
        finish_reason = None
        final_usage = None

        queue: asyncio.Queue[str | None] = asyncio.Queue()

        def _sync_stream() -> None:
            nonlocal finish_reason, final_usage
            try:
                response = self.client.chat.completions.create(**kwargs)
                for chunk in response:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    content = getattr(delta, "content", None)
                    if content:
                        queue.put_nowait(content)
                    if chunk.choices[0].finish_reason is not None:
                        finish_reason = chunk.choices[0].finish_reason
                    if hasattr(chunk, "usage") and chunk.usage:
                        final_usage = chunk.usage
            except Exception as exc:
                logger.error("Z.ai streaming error: %s", exc)
            finally:
                queue.put_nowait(None)

        asyncio.get_event_loop().run_in_executor(None, _sync_stream)

        while True:
            chunk = await queue.get()
            if chunk is None:
                elapsed = time.monotonic() - t0
                # Extract cached tokens from streaming usage
                cached_tokens = None
                if final_usage and hasattr(final_usage, "prompt_tokens_details"):
                    details = final_usage.prompt_tokens_details
                    if details and hasattr(details, "cached_tokens"):
                        cached_tokens = details.cached_tokens
                logger.info(
                    "Z.ai stream: model=%s latency=%.2fs ttft=%.2fs "
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
                break
            if first_token_time is None:
                first_token_time = time.monotonic()
            chunk_count += 1
            total_chars += len(chunk)
            yield chunk

    async def quick_prompt(self, prompt: str) -> str:
        """Send a bare prompt string and return the response."""
        return await self.chat(
            messages=[{"role": "user", "content": prompt}],
        )


def _raise_zai_error(exc: Exception) -> NoReturn:
    """Re-raise Z.ai SDK errors with context."""
    if _zai_module is not None:
        from zai.core import APIStatusError, APITimeoutError

        if isinstance(exc, (APIStatusError, APITimeoutError)):
            raise RuntimeError(f"Z.ai API error: {exc}") from exc
    raise RuntimeError(f"Z.ai call failed: {exc}") from exc
