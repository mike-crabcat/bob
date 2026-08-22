"""Tier 2 actionability probe (Bob3 Phase III item 4).

Ported from the patience gate's relevance prompt. Runs ONLY at window close
and ONLY for batches with no addressed stimulus (structural ACT bypasses it).
Returns one of:

- ``ACT``        — dispatch the batch to the main LLM now.
- ``WAIT``       — conversation still flowing; extend the window once.
- ``STAND_DOWN`` — banter/addressed-to-others; flush without the main LLM.

Failure policy: any error (context build, LLM call, parse) returns ACT —
silence must never be caused by probe infrastructure.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

VALID = ("ACT", "WAIT", "STAND_DOWN")


def probe_system_prompt(bot_name: str) -> str:
    return f"""\
You are an attention gate for a chatbot named "{bot_name}". A batch of group-chat
messages has arrived, none of which structurally addresses {bot_name} (no @mention,
no name, no reply to {bot_name}). Decide whether {bot_name} should respond.

Decisions:
- STAND_DOWN (clear skip cases):
  * Messages are grammatically addressed TO another specific person ("hey david", \
"sarah what's up?", "@tina ...") — the addressee is that person, not {bot_name}.
  * Casual banter, jokes, or reactions between other people ("lol", "nice", "haha", \
an emoji) with no question or request.
- ACT (respond now):
  * A direct question to the group as a whole that {bot_name} would naturally answer — \
a factual query, a request for help, a follow-up on a thread {bot_name} started.
  * {bot_name} was asked something in a recent message that hasn't been answered yet.
  * The latest message is a follow-up to a thread {bot_name} was part of in the last \
few turns — callbacks, references to a place/topic {bot_name} just discussed.
  * A message mentions third parties by name ("Audrey and Mabel like swimming", \
"tell David") but is aimed at the group/{bot_name} — names inside the message body \
are topics, not addressees.
- WAIT (defer, re-evaluate shortly):
  * The sender appears mid-thought (fragments, trailing "...", incomplete sentences).
  * Multiple people are actively chatting and the thread hasn't settled.

When ambiguous: ask "would a person in the group naturally expect {bot_name} to reply?" \
Consider the Session context — if {bot_name} has an active role (assistant, planner, \
coordinator), lean toward ACT for questions and follow-ups. Default to STAND_DOWN only \
for reactions and explicit addressing of another named participant.

Respond with ONLY a JSON object: {{"decision": "ACT"|"WAIT"|"STAND_DOWN", "reason": "<brief>"}}"""


async def probe_actionability(
    ctx: Any,
    session_key: str,
    *,
    bot_name: str = "Bob",
    model: str = "gpt-5.6-luna",
    max_context_messages: int = 10,
) -> str:
    try:
        context_text = await _build_context(ctx, session_key, max_context_messages,
                                            bot_name=bot_name)
    except Exception:
        logger.warning("tier2: context build failed for %s, defaulting ACT",
                       session_key, exc_info=True)
        return "ACT"

    try:
        from bob_server.services.llm_dispatch import LLMDispatchService

        result = await LLMDispatchService(ctx).chat(
            [{"role": "system", "content": probe_system_prompt(bot_name)},
             {"role": "user", "content": context_text}],
            model=model,
            temperature=0.0,
            max_tokens=200,
            reasoning_effort="low",
            call_category="attention_probe",
            session_key=session_key,
        )
    except Exception:
        logger.warning("tier2: LLM call failed for %s, defaulting ACT",
                       session_key, exc_info=True)
        return "ACT"

    try:
        parsed = json.loads(result.strip())
        decision = str(parsed.get("decision", "ACT")).upper()
        reason = parsed.get("reason", "?")
        if decision not in VALID:
            decision = "ACT"
        logger.info("tier2: %s for %s (reason: %s)", decision, session_key, reason)
        return decision
    except (json.JSONDecodeError, AttributeError, ValueError, TypeError):
        upper = result.upper()
        for cand in ("STAND_DOWN", "WAIT", "ACT"):
            if cand in upper:
                logger.info("tier2: raw-parse %s for %s", cand, session_key)
                return cand
        logger.warning("tier2: unparseable probe response for %s, defaulting ACT: %s",
                       session_key, result[:100])
        return "ACT"


async def _build_context(ctx: Any, session_key: str, max_context: int,
                         *, bot_name: str = "Bob") -> str:
    """Session agenda + recent dispatched dialogue + the pending batch."""
    from bob_server.repositories.history import HistoryRepository
    from bob_server.services.session_agenda_service import SessionAgendaService

    parts: list[str] = []

    agenda = await SessionAgendaService(ctx).get_agenda(session_key)
    if agenda and agenda.strip():
        parts.append("## Session context")
        parts.append(agenda.strip())

    repo = HistoryRepository(ctx.db)
    rows = await repo.recent_dialogue(session_key, limit=max_context,
                                      dispatched_only=True)
    if rows:
        parts.append("## Recent conversation")
        for row in rows:
            role = "User" if row["role"] == "user" else "Bot"
            parts.append(f"{role}: {(row['content'] or '')[:200]}")

    pending = await repo.recent_dialogue(session_key, limit=10,
                                         dispatched_only=False,
                                         pending_only=True)
    if pending:
        parts.append("## Pending unprocessed messages")
        for row in pending:
            parts.append((row["content"] or "")[:300])

    parts.append("## Decision")
    parts.append(f"Should {bot_name} respond to the pending batch? "
                 "Reply with the JSON decision object.")
    return "\n".join(parts)
