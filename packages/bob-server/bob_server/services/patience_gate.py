"""Patience gate — decides when to dispatch using a fast LLM evaluation."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Coroutine

from bob_server.services.patience_buffer import PatienceBuffer, PatienceBufferRegistry, PendingItem

logger = logging.getLogger(__name__)

# Track sessions with an active dispatch to avoid queueing duplicate dispatches
_dispatching_sessions: set[str] = set()


@dataclass
class PatienceDecision:
    """Output of the patience LLM evaluation.

    `respond=False` means skip the main dispatch entirely for this batch
    (relevance gate). `wait_seconds` is only meaningful when `respond=True`.
    """
    respond: bool
    wait_seconds: float
    reason: str


def _patience_system_prompt(bot_name: str, *, relevance_gating: bool = False) -> str:
    timing_rules = f"""\
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
- Default to 10 seconds if uncertain"""

    if not relevance_gating:
        return f"""\
You are a message timing assistant. You decide how long a chatbot named "{bot_name}" should wait \
before responding to the latest batch of messages. You return a wait duration in seconds based on \
the conversation context.

{timing_rules}

Respond with ONLY a JSON object: {{"wait_seconds": <number>, "reason": "<brief explanation>"}}"""

    return f"""\
You are a message timing and relevance assistant for a chatbot named "{bot_name}". You make two \
decisions about the latest batch of messages: (1) whether {bot_name} should respond at all, and \
(2) if so, how long to wait.

{timing_rules}

Relevance rules (decide `respond`):
- respond=false (clear skip cases):
  * Message is grammatically addressed TO another specific person ("hey david", "sarah what's up?", \
"@tina ...") — i.e. the addressee is that person, not {bot_name}.
  * Casual banter, jokes, or reactions between other people ("lol", "nice", "haha", an emoji) \
with no question or request.
- Mention is not address (DO NOT skip just because a name appears):
  * A message that mentions third parties by name ("Audrey and Mabel like swimming", \
"tell David", "Mike asked for it") but is grammatically aimed at {bot_name} — a question, \
an imperative, or "you" — is directed at {bot_name}. Names inside the message body are \
topics, not addressees.
- respond=true (clear respond cases):
  * {bot_name} is addressed by name ("{bot_name}, ...", "hey {bot_name}", "@{bot_name}").
  * A direct question to the group as a whole that {bot_name} would naturally answer — a factual \
query, a request for help, a follow-up on a thread {bot_name} started.
  * {bot_name} was asked something in a recent message that hasn't been answered yet.
  * The latest message is a follow-up to a thread {bot_name} was part of in the last few turns — \
callbacks like "back to X", references to a place/topic {bot_name} just discussed, or a \
follow-up question on the same subject.
- When ambiguous: ask "would a person in the group naturally expect {bot_name} to reply to this?" \
Consider the Session context below — if {bot_name} has an active role in this session \
(assistant, planner, coordinator), lean toward responding to questions and follow-ups. \
{bot_name} speaking unbidden is worse than {bot_name} being briefly silent. \
Default to respond=false only for reactions ("lol", "nice", emoji) and explicit addressing of \
another named participant; otherwise lean toward respond=true.

Respond with ONLY a JSON object: {{"respond": <true|false>, "wait_seconds": <number>, "reason": "<brief>"}}"""


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
    relevance_gating_enabled: bool = False,
    patience_enabled: bool = True,
    settle_seconds: float = 1.5,
) -> None:
    """Submit an item to the patience buffer. May trigger immediate, deferred, or no dispatch.

    Both modes batch through the buffer:
    - `patience_enabled=True`: runs the patience LLM gate (timing + optional
      relevance). When `relevance_gating_enabled` is also True and the LLM
      returns `respond=false`, the buffered messages are marked dispatched
      without invoking `dispatch_fn` — i.e. the main LLM is skipped for that
      batch.
    - `patience_enabled=False`: skips the LLM and uses a fixed `settle_seconds`
      delay to absorb bursts. Relevance gating is ignored (no LLM = no
      relevance decision). Always dispatches eventually.
    """

    buffer = PatienceBufferRegistry.get(session_key)
    buffer.add(item)
    buffer.cancel_timer()

    pending_count = len([i for i in buffer.items if i.item_type == "message"])
    typing_count = len([i for i in buffer.items if i.item_type == "typing"])
    logger.info(
        "patience: new %s item for %s from %s, buffer=%d messages + %d typing (patience=%s relevance=%s)",
        item.item_type, session_key, item.sender_name, pending_count, typing_count,
        patience_enabled, relevance_gating_enabled,
    )

    # If a dispatch is already in progress, just buffer the item — no evaluation needed.
    # The in-progress dispatch will claim these messages via mark_dispatched.
    if session_key in _dispatching_sessions:
        logger.info("patience: dispatch in progress for %s, buffering silently", session_key)
        return

    # Safety cap — too many MESSAGES buffered (typing doesn't count).
    # If the last relevance decision was "skip", flush without dispatching so
    # an off-topic burst doesn't force a main-LLM call we already decided not
    # to make. Otherwise force dispatch as today.
    if pending_count >= max_pending_items:
        if relevance_gating_enabled and patience_enabled and buffer.last_respond is False:
            logger.info(
                "patience buffer cap hit for %s (%d messages) after skip decision, flushing without dispatch",
                session_key, pending_count,
            )
            await _flush_without_dispatch(ctx, session_key)
            return
        logger.info("patience buffer cap hit for %s (%d messages), forcing dispatch", session_key, pending_count)
        await _dispatch_and_cleanup(session_key, dispatch_fn)
        return

    # Gate decision: LLM eval when patience is on, fixed delay when off.
    if patience_enabled:
        decision = await _evaluate_urgency(
            ctx, session_key, buffer, bot_name, model, max_context_messages,
            relevance_gating=relevance_gating_enabled,
        )
    else:
        decision = PatienceDecision(
            respond=True,
            wait_seconds=max(0.0, settle_seconds),
            reason="patience-off settle",
        )

    # Effective respond flag: when relevance gating is off (or patience itself
    # is off), always respond. Skip path is only meaningful when both flags are on.
    relevance_active = patience_enabled and relevance_gating_enabled
    effective_respond = decision.respond or not relevance_active

    # Mark this batch as evaluated so future patience contexts exclude it.
    # (Pending semantics: items older than this instant are no longer "pending"
    # for the patience LLM — only newly-arrived items get re-fed.)
    buffer.last_evaluated_at = buffer.last_activity
    buffer.last_respond = effective_respond

    # Skip path: relevance gate said don't respond. Mark messages dispatched
    # (so they don't sit claiming future dispatches), don't set a timer, don't
    # invoke dispatch_fn. The next arriving message will re-run the patience LLM.
    if not effective_respond:
        logger.info(
            "patience: skip for %s (reason: %s), %d messages marked dispatched without main LLM",
            session_key, decision.reason, pending_count,
        )
        await _flush_without_dispatch(ctx, session_key)
        return

    # If new items arrived while we were evaluating, skip setting timer
    if buffer.last_activity > item.timestamp:
        logger.info("patience: new activity during evaluation for %s, skipping timer", session_key)
        return

    # If a dispatch started while we were evaluating, back off
    if session_key in _dispatching_sessions:
        logger.info("patience: dispatch started during evaluation for %s, backing off", session_key)
        return

    logger.info(
        "patience: timer=%.1fs for %s (reason: %s)",
        decision.wait_seconds, session_key, decision.reason,
    )

    loop = asyncio.get_running_loop()
    buffer.timer_handle = loop.call_later(
        decision.wait_seconds,
        lambda: asyncio.ensure_future(_timer_fired(session_key, dispatch_fn)),
    )


async def _timer_fired(session_key: str, dispatch_fn: Callable[[], Coroutine]) -> None:
    """Called when the patience timer expires."""
    buffer = PatienceBufferRegistry.get(session_key)
    buffer.timer_handle = None
    if session_key in _dispatching_sessions:
        logger.info("patience timer fired for %s but dispatch already in progress, skipping", session_key)
        return
    logger.info("patience timer fired for %s, dispatching", session_key)
    await _dispatch_and_cleanup(session_key, dispatch_fn)


async def _dispatch_and_cleanup(session_key: str, dispatch_fn: Callable[[], Coroutine]) -> None:
    """Run dispatch, track state, and clean up buffer afterward."""
    _dispatching_sessions.add(session_key)
    try:
        await dispatch_fn()
    finally:
        _dispatching_sessions.discard(session_key)
        # Clear buffer after dispatch — messages were claimed by mark_dispatched
        buffer = PatienceBufferRegistry.get(session_key)
        buffer.clear()
        logger.info("patience: buffer cleared for %s after dispatch", session_key)


async def _flush_without_dispatch(ctx: Any, session_key: str) -> None:
    """Mark buffered messages as dispatched without running the main LLM.

    Used by the relevance skip path and the post-skip safety cap. Messages are
    taken out of the pending queue (`session_messages.dispatched = 1`) so future
    dispatches only see newly-arrived items. Memory extraction is unaffected —
    `run_silent_turn_extraction` reads `session_messages` regardless of the
    `dispatched` flag.
    """
    from bob_server.services.session_service import SessionService

    claimed = await SessionService(ctx).mark_dispatched(session_key)
    buffer = PatienceBufferRegistry.get(session_key)
    buffer.clear()
    logger.info(
        "patience: flushed %d message(s) for %s without main LLM (skip)",
        claimed, session_key,
    )



async def _evaluate_urgency(
    ctx: Any,
    session_key: str,
    buffer: PatienceBuffer,
    bot_name: str,
    model: str,
    max_context_messages: int,
    *,
    relevance_gating: bool = False,
) -> PatienceDecision:
    """Ask the fast LLM how long to wait (and, with relevance gating, whether to respond)."""

    try:
        from bob_server.services.session_agenda_service import SessionAgendaService

        agenda = await SessionAgendaService(ctx).get_agenda(session_key)
        context_text = await _build_patience_context(
            ctx.db, session_key, buffer, max_context_messages, agenda=agenda,
        )
    except Exception:
        logger.warning("patience: failed to build context, defaulting to 3s respond=true", exc_info=True)
        return PatienceDecision(respond=True, wait_seconds=3.0, reason="context-build-failed")

    try:
        from bob_server.services.llm_dispatch import LLMDispatchService

        svc = LLMDispatchService(ctx)
        result = await svc.chat(
            [{"role": "system", "content": _patience_system_prompt(bot_name, relevance_gating=relevance_gating)},
             {"role": "user", "content": context_text}],
            model=model,
            temperature=0.0,
            max_tokens=50,
            call_category="patience_check",
            session_key=session_key,
        )
    except Exception:
        logger.warning("patience: LLM call failed, defaulting to 3s respond=true", exc_info=True)
        return PatienceDecision(respond=True, wait_seconds=3.0, reason="llm-call-failed")

    try:
        parsed = json.loads(result.strip())
        wait_seconds = float(parsed.get("wait_seconds", 10))
        reason = parsed.get("reason", "?")
        # When relevance gating is off, always respond (preserve legacy behavior).
        # When on, honor the LLM's `respond` field but default to True on miss.
        respond = bool(parsed.get("respond", True)) if relevance_gating else True
        wait_seconds = max(0, min(wait_seconds, 60))
        logger.info(
            "patience LLM decided respond=%s wait=%.0fs for %s (reason: %s)",
            respond, wait_seconds, session_key, reason,
        )
        return PatienceDecision(respond=respond, wait_seconds=wait_seconds, reason=reason)
    except (json.JSONDecodeError, AttributeError, ValueError, TypeError):
        import re
        nums = re.findall(r'\d+\.?\d*', result)
        if nums:
            wait_seconds = max(0, min(float(nums[0]), 60))
            logger.info("patience LLM raw parse: %.0fs for %s", wait_seconds, session_key)
            return PatienceDecision(respond=True, wait_seconds=wait_seconds, reason="raw-parse")
        logger.warning("patience: couldn't parse LLM response, defaulting to 10s: %s", result[:100])
        return PatienceDecision(respond=True, wait_seconds=10.0, reason="parse-failed")


async def _build_patience_context(
    db: Any,
    session_key: str,
    buffer: PatienceBuffer,
    max_context: int,
    *,
    agenda: str | None = None,
) -> str:
    """Build a short text summary for the patience LLM."""

    parts: list[str] = []

    # Stored session agenda — gives the LLM Bob's role/purpose in this session
    # (e.g. "family-assistant for Mike's Chamonix trip") so it can distinguish
    # messages that mention third parties from messages addressed to them.
    if agenda and agenda.strip():
        parts.append("## Session context")
        parts.append(agenda.strip())

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

    # Pending unprocessed messages — only items that arrived AFTER the last
    # patience evaluation. Items at or before `last_evaluated_at` have already
    # been considered and don't count as "pending" for the next decision.
    messages = [
        i for i in buffer.items
        if i.item_type == "message" and i.timestamp > buffer.last_evaluated_at
    ]
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
