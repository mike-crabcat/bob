"""Unified LLM dispatch service — routes to providers and logs all interactions."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from cyborg_server.services.tools import Tool

from cyborg_server.services.base import BaseService

logger = logging.getLogger(__name__)


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
    provider: str,
    model: str,
    call_category: str,
    session_key: str | None = None,
    system_prompt: str = "",
    user_message: str = "",
    messages_json: str | None = None,
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
) -> None:
    """Record an LLM call to the unified log. Non-blocking."""
    try:
        await db.execute(
            """INSERT INTO llm_call_log
               (id, provider, model, call_category, session_key,
                system_prompt, user_message, messages_json,
                response_text, latency_seconds, ttft_seconds,
                prompt_tokens, completion_tokens, total_tokens, cached_tokens,
                status, error_message, project_id, task_id, dispatch_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid4()), provider, model, call_category, session_key,
                system_prompt, user_message, messages_json,
                response_text, latency_seconds, ttft_seconds,
                prompt_tokens, completion_tokens, total_tokens, cached_tokens,
                status, error_message, project_id, task_id, dispatch_id,
            ),
        )
    except Exception:
        logger.warning("Failed to record LLM call log", exc_info=True)


class LLMDispatchService(BaseService):
    """Routes LLM calls to providers and logs all interactions."""

    def _resolve_provider(self, provider: str | None = None) -> str:
        """Resolve which provider to use."""
        if provider:
            return provider
        settings = self._get_settings()
        if settings.openai.enabled:
            return "openai"
        if settings.zai.enabled:
            return "zai"
        raise RuntimeError("No LLM provider configured. Set CYBORG_OPENAI_API_KEY or CYBORG_ZAI_API_KEY.")

    def _get_provider_service(self, provider: str) -> Any:
        """Get the provider service instance."""
        if provider == "openai":
            from cyborg_server.services.openai_service import OpenAIService
            return OpenAIService(self.ctx)
        if provider == "zai":
            from cyborg_server.services.zai_service import ZaiService
            return ZaiService(self.ctx)
        raise ValueError(f"Unknown LLM provider: {provider}")

    def _resolve_model(self, provider: str, model: str | None = None) -> str:
        """Resolve model for the given provider."""
        if model:
            return model
        settings = self._get_settings()
        if provider == "openai":
            return settings.openai.default_model
        if provider == "zai":
            return settings.zai.default_model
        raise ValueError(f"Unknown provider: {provider}")

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        provider: str | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        call_category: str = "quick_prompt",
        session_key: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        dispatch_id: str | None = None,
    ) -> str:
        """Non-streaming chat completion with automatic logging."""
        resolved_provider = self._resolve_provider(provider)
        resolved_model = self._resolve_model(resolved_provider, model)
        service = self._get_provider_service(resolved_provider)

        system_prompt, user_message = _extract_from_messages(messages)
        messages_json = json.dumps(messages)
        t0 = time.monotonic()

        try:
            result = await service.chat(
                messages=messages,
                model=resolved_model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            elapsed = time.monotonic() - t0

            await _record_log(
                self.db,
                provider=resolved_provider,
                model=resolved_model,
                call_category=call_category,
                session_key=session_key,
                system_prompt=system_prompt,
                user_message=user_message,
                messages_json=messages_json,
                response_text=result or "",
                latency_seconds=elapsed,
                status="completed",
                project_id=project_id,
                task_id=task_id,
                dispatch_id=dispatch_id,
            )

            logger.info(
                "LLM dispatch: provider=%s model=%s category=%s latency=%.2fs "
                "input_chars=%d output_chars=%d",
                resolved_provider, resolved_model, call_category, elapsed,
                sum(len(m.get("content", "")) for m in messages),
                len(result or ""),
            )
            return result

        except Exception as exc:
            elapsed = time.monotonic() - t0
            logger.error("LLM dispatch failed: provider=%s model=%s error=%s", resolved_provider, resolved_model, exc)
            await _record_log(
                self.db,
                provider=resolved_provider,
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
            )
            raise

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        provider: str | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        call_category: str = "quick_prompt",
        session_key: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        dispatch_id: str | None = None,
    ) -> AsyncIterator[str]:
        """Streaming chat completion with automatic logging."""
        resolved_provider = self._resolve_provider(provider)
        resolved_model = self._resolve_model(resolved_provider, model)
        service = self._get_provider_service(resolved_provider)

        system_prompt, user_message = _extract_from_messages(messages)
        messages_json = json.dumps(messages)

        # For OpenAI, use StreamResult to capture token counts
        stream_result: Any = None
        if resolved_provider == "openai":
            from cyborg_server.services.openai_service import StreamResult
            stream_result = StreamResult()

        t0 = time.monotonic()
        ttft: float | None = None
        accumulated = ""

        try:
            kwargs: dict[str, Any] = {
                "messages": messages,
                "model": resolved_model,
                "temperature": temperature,
            }
            if max_tokens is not None:
                kwargs["max_tokens"] = max_tokens

            if stream_result is not None:
                kwargs["stream_result"] = stream_result

            async for chunk in service.chat_stream(**kwargs):
                if chunk:
                    if ttft is None:
                        ttft = time.monotonic() - t0
                    accumulated += chunk
                    yield chunk

            elapsed = time.monotonic() - t0

            # Extract token counts from StreamResult if available
            prompt_tokens = None
            completion_tokens = None
            total_tokens = None
            cached_tokens = None
            if stream_result is not None:
                prompt_tokens = stream_result.prompt_tokens
                completion_tokens = stream_result.completion_tokens
                total_tokens = stream_result.total_tokens
                cached_tokens = stream_result.cached_tokens

            await _record_log(
                self.db,
                provider=resolved_provider,
                model=resolved_model,
                call_category=call_category,
                session_key=session_key,
                system_prompt=system_prompt,
                user_message=user_message,
                messages_json=messages_json,
                response_text=accumulated,
                latency_seconds=elapsed,
                ttft_seconds=ttft,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cached_tokens=cached_tokens,
                status="completed",
                project_id=project_id,
                task_id=task_id,
                dispatch_id=dispatch_id,
            )

            logger.info(
                "LLM dispatch stream: provider=%s model=%s category=%s latency=%.2fs ttft=%.2fs "
                "input_chars=%d output_chars=%d tokens=%s",
                resolved_provider, resolved_model, call_category, elapsed, ttft or 0,
                sum(len(m.get("content", "")) for m in messages),
                len(accumulated),
                total_tokens,
            )

        except Exception as exc:
            elapsed = time.monotonic() - t0
            logger.error("LLM dispatch stream failed: provider=%s model=%s error=%s", resolved_provider, resolved_model, exc)
            await _record_log(
                self.db,
                provider=resolved_provider,
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
            )
            raise

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[Tool],
        *,
        provider: str | None = None,
        model: str | None = None,
        max_iterations: int = 10,
        call_category: str = "tool_call",
        session_key: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        dispatch_id: str | None = None,
    ) -> str:
        """Chat with tool calling. Loops until LLM finishes or max iterations.

        The caller provides a list of Tool objects (created via @tool decorator)
        and this method handles the multi-turn tool call loop automatically.
        """
        resolved_provider = self._resolve_provider(provider)
        resolved_model = self._resolve_model(resolved_provider, model)
        service = self._get_provider_service(resolved_provider)

        system_prompt, user_message = _extract_from_messages(messages)
        initial_messages_json = json.dumps(messages)

        # Convert tools to provider format
        openai_tools = [t.to_openai_format() for t in tools]
        tool_handlers = {t.name: t.handler for t in tools}

        t0 = time.monotonic()
        try:
            from cyborg_server.services.openai_service import StreamResult
            stream_result = StreamResult()

            result = await service.chat_with_tools(
                messages=messages,
                tools=openai_tools,
                tool_handlers=tool_handlers,
                model=resolved_model,
                max_iterations=max_iterations,
                stream_result=stream_result,
            )
            elapsed = time.monotonic() - t0

            await _record_log(
                self.db,
                provider=resolved_provider,
                model=resolved_model,
                call_category=call_category,
                session_key=session_key,
                system_prompt=system_prompt,
                user_message=user_message,
                messages_json=initial_messages_json,
                response_text=result,
                latency_seconds=elapsed,
                prompt_tokens=stream_result.prompt_tokens,
                completion_tokens=stream_result.completion_tokens,
                total_tokens=stream_result.total_tokens,
                cached_tokens=stream_result.cached_tokens,
                status="completed",
                project_id=project_id,
                task_id=task_id,
                dispatch_id=dispatch_id,
            )

            logger.info(
                "LLM dispatch tools: provider=%s model=%s category=%s latency=%.2fs "
                "tools=%d output_chars=%d tokens=%s",
                resolved_provider, resolved_model, call_category, elapsed,
                len(tools), len(result),
                stream_result.total_tokens,
            )
            return result

        except Exception as exc:
            elapsed = time.monotonic() - t0
            logger.error("LLM dispatch tools failed: provider=%s model=%s error=%s", resolved_provider, resolved_model, exc)
            await _record_log(
                self.db,
                provider=resolved_provider,
                model=resolved_model,
                call_category=call_category,
                session_key=session_key,
                system_prompt=system_prompt,
                user_message=user_message,
                messages_json=initial_messages_json,
                latency_seconds=elapsed,
                status="failed",
                error_message=str(exc),
                project_id=project_id,
                task_id=task_id,
                dispatch_id=dispatch_id,
            )
            raise

    async def chat_stream_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[Tool],
        *,
        provider: str | None = None,
        model: str | None = None,
        max_iterations: int = 10,
        call_category: str = "voice_chat",
        session_key: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        dispatch_id: str | None = None,
    ) -> AsyncIterator[str]:
        """Stream chat with tool calling support.

        Handles the tool loop non-streamingly (tool calls need full responses),
        then streams the final text response for real-time TTS/consumption.
        """
        resolved_provider = self._resolve_provider(provider)
        resolved_model = self._resolve_model(resolved_provider, model)
        service = self._get_provider_service(resolved_provider)

        openai_tools = [t.to_openai_format() for t in tools]
        tool_handlers = {t.name: t.handler for t in tools}

        # Tool loop: non-streaming rounds until LLM gives a text response
        for iteration in range(max_iterations):
            response = await service.client.chat.completions.create(
                model=resolved_model,
                messages=messages,
                tools=openai_tools,
            )
            message = response.choices[0].message

            if not message.tool_calls:
                # No tool calls — yield the final text and return
                final_text = message.content or ""
                if final_text:
                    yield final_text
                return

            # Execute tool calls and append to messages
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

            logger.info(
                "chat_stream_with_tools: iteration=%d tool_calls=%d",
                iteration + 1,
                len(message.tool_calls),
            )

        # Hit max iterations — make one final streaming call
        logger.warning("chat_stream_with_tools hit max iterations: %d", max_iterations)
        async for chunk in service.chat_stream(
            messages=messages,
            model=resolved_model,
        ):
            if chunk:
                yield chunk
