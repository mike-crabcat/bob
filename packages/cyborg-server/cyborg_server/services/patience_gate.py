"""Patience gate — decides when to dispatch using a fast LLM evaluation."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Coroutine

from cyborg_server.services.patience_buffer import PatienceBuffer, PatienceBufferRegistry, PendingItem

logger = logging.getLogger(__name__)


def _patience_system_prompt(bot_name: str) -> str:
    return f"""\
You are a message timing assistant. You decide how long a chatbot named "{bot_name}" should wait \
before responding to the latest batch of messages. You return a wait duration in seconds based on \
the conversation context.

Rules:
- Direct @mention or urgent question ("{bot_name}?", "help!", emergency): 0 seconds
- Direct question expecting an answer: 3 seconds
- Normal conversational message, complete thought: 8–12 seconds
- User appears to be composing a multi-part message (short fragments, trailing "...", \
incomplete sentences): 15 seconds (they're still going)
- Multiple users actively chatting (rapid messages from different people, typing indicators): 20 seconds
- Active debate/banter where the bot isn't directly addressed: 45 seconds
- The bot is NOT directly addressed, topic is casual, and the conversation is flowing: 60 seconds

Important:
- If the bot's name is mentioned ("{bot_name}?", "hey {bot_name}"), treat as directly addressed → 0 seconds
- If a previous message asked the bot something and no response has come yet, respond now → 0 seconds
- Default to 10 seconds if uncertain

Respond with ONLY a JSON object: {{"wait_seconds": <number>, "reason": "<brief explanation>"}}"""


async def submit_to_patience(
    ctx: Any,
    session_key: str,
    item: PendingItem,
    dispatch_fn: Callable[[], Coroutine],
    *,
    bot_name: str = "Bob",
    model: str = "gpt-5.4-mini",
    max_pending_items: int = 20,
    max_context_messages: int = 10,
) -> None:
    """Submit an item to the patience buffer. May trigger immediate or deferred dispatch."""

    buffer = PatienceBufferRegistry.get(session_key)
    buffer.add(item)
    buffer.cancel_timer()

    loop = asyncio.get_running_loop()

    pending_count = len([i for i in buffer.items if i.item_type == "message"])
    typing_count = len([i for i in buffer.items if i.item_type == "typing"])
    logger.info(
        "patience: new %s item for %s from %s, buffer=%d messages + %d typing",
        item.item_type, session_key, item.sender_name, pending_count, typing_count,
    )

    # Safety cap — force dispatch if buffer is too large
    if len(buffer.items) >= max_pending_items:
        logger.info("patience buffer cap hit for %s (%d items), forcing dispatch", session_key, len(buffer.items))
        _fire_dispatch(buffer, dispatch_fn)
        return

    # Evaluate urgency with the fast LLM — it returns a recommended wait duration
    wait_seconds = await _evaluate_urgency(ctx, session_key, buffer, bot_name, model, max_context_messages)

    # If new items arrived while we were evaluating, cancel and re-evaluate
    if buffer.last_activity > item.timestamp:
        logger.info("patience: new activity during evaluation for %s, skipping timer", session_key)
        return

    logger.info("patience: timer=%.1fs for %s", wait_seconds, session_key)

    buffer.timer_handle = loop.call_later(
        wait_seconds,
        lambda: asyncio.ensure_future(_timer_fired(session_key, dispatch_fn)),
    )


async def _timer_fired(session_key: str, dispatch_fn: Callable[[], Coroutine]) -> None:
    """Called when the patience timer expires."""
    buffer = PatienceBufferRegistry.get(session_key)
    buffer.timer_handle = None
    logger.info("patience timer fired for %s, dispatching", session_key)
    await dispatch_fn()


def _fire_dispatch(
    buffer: PatienceBuffer,
    dispatch_fn: Callable[[], Coroutine],
) -> None:
    """Immediately schedule the dispatch."""
    buffer.cancel_timer()
    asyncio.ensure_future(dispatch_fn())


async def _evaluate_urgency(
    ctx: Any,
    session_key: str,
    buffer: PatienceBuffer,
    bot_name: str,
    model: str,
    max_context_messages: int,
) -> float:
    """Ask the fast LLM how long to wait. Returns seconds to wait."""

    try:
        context_text = await _build_patience_context(ctx.db, session_key, buffer, max_context_messages)
    except Exception:
        logger.warning("patience: failed to build context, defaulting to 3s", exc_info=True)
        return 3.0

    try:
        from cyborg_server.services.llm_dispatch import LLMDispatchService

        svc = LLMDispatchService(ctx)
        result = await svc.chat(
            [{"role": "system", "content": _patience_system_prompt(bot_name)}, {"role": "user", "content": context_text}],
            model=model,
            temperature=0.0,
            max_tokens=50,
            call_category="patience_check",
            session_key=session_key,
        )
    except Exception:
        logger.warning("patience: LLM call failed, defaulting to 3s", exc_info=True)
        return 3.0

    try:
        parsed = json.loads(result.strip())
        wait_seconds = float(parsed.get("wait_seconds", 10))
        reason = parsed.get("reason", "?")
        # Clamp to reasonable range
        wait_seconds = max(0, min(wait_seconds, 60))
        logger.info("patience LLM decided %.0fs for %s (reason: %s)", wait_seconds, session_key, reason)
        return wait_seconds
    except (json.JSONDecodeError, AttributeError, ValueError, TypeError):
        # Try to extract a number from raw text
        import re
        nums = re.findall(r'\d+\.?\d*', result)
        if nums:
            wait_seconds = max(0, min(float(nums[0]), 60))
            logger.info("patience LLM raw parse: %.0fs for %s", wait_seconds, session_key)
            return wait_seconds
        logger.warning("patience: couldn't parse LLM response, defaulting to 10s: %s", result[:100])
        return 10.0


async def _build_patience_context(
    db: Any,
    session_key: str,
    buffer: PatienceBuffer,
    max_context: int,
) -> str:
    """Build a short text summary for the patience LLM."""

    parts: list[str] = []

    # Recent dispatched messages
    rows = await db.fetch_all(
        "SELECT role, content, sender_id FROM session_messages "
        "WHERE session_key = ? AND role IN ('user', 'assistant') AND dispatched = 1 "
        "ORDER BY created_at DESC LIMIT ?",
        (session_key, max_context),
    )
    if rows:
        parts.append("## Recent conversation")
        for row in reversed(rows):
            role = "User" if row["role"] == "user" else "Bot"
            content = (row["content"] or "")[:200]
            parts.append(f"{role}: {content}")

    # Pending unprocessed messages
    messages = [i for i in buffer.items if i.item_type == "message"]
    if messages:
        parts.append("## Pending unprocessed messages")
        for msg in messages[-10:]:
            parts.append(f"{msg.sender_name}: {msg.payload.get('text', '')[:200]}")

    # Active typing indicators
    typing = [i for i in buffer.items if i.item_type == "typing"]
    if typing:
        typing_names = list({t.sender_name for t in typing if t.sender_name})
        if typing_names:
            parts.append("## Active typing")
            parts.append(", ".join(typing_names) + " is typing...")

    parts.append("## Decision")
    parts.append("How many seconds should the bot wait before responding?")

    return "\n".join(parts)
