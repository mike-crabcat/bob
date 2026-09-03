"""Tier 2 actionability probe (Bob3 Phase III item 4).

Ported from the patience gate's relevance prompt. Runs ONLY at window close
and ONLY for batches with no addressed stimulus (structural ACT bypasses it).
Returns one of:

- ``ACT``        — dispatch the batch to the main LLM now.
- ``WAIT``       — conversation still flowing; extend the window once.
- ``STAND_DOWN`` — banter/addressed-to-others; flush without the main LLM.

Policy (2026-09-03 tightening): {bot} speaks only when ENGAGED — mid-exchange
or plainly the natural answerer. The ambiguous default is STAND_DOWN, not
ACT: unaddressed group chatter gets silence unless engagement clearly holds.
An explicit Engagement state line (computed from history, not guessed by the
model) carries how long since the bot last spoke.

Reactions (2026-09-03): on STAND_DOWN the probe may additionally recommend a
reaction clip — a rare, high-bar decoration of silence (a win announced, a
glorious failure, real drama), never a replacement for a reply. The
coordinator enforces the clip registry, per-chat cooldown, and the
``BOB_PROBE_REACTIONS=off`` kill switch; ``react`` is None unless the
decision is STAND_DOWN.

Failure policy: any error (context build, LLM call, parse) returns ACT —
silence must never be caused by probe infrastructure.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from server.services.dispatch_runner import is_no_reply

logger = logging.getLogger(__name__)

VALID = ("ACT", "WAIT", "STAND_DOWN")

# Reaction clips the probe may recommend (name → what it means). Keep in
# sync with the manifest in self/bob/identity.md — files live at
# self/bob/avatar/reactions/<name>.mp4 in the workspace (healed from the
# repo bundle at boot, so the list is stable across instances).
REACTION_CLIPS: dict[str, str] = {
    "bob-celebrate": "something finally worked — a win landed",
    "bob-patience-v2": "deadpan waiting, eye twitch",
    "bob-this-is-fine": "it is not fine",
    "bob-popcorn-cinema": "watching drama unfold",
    "bob-awkward-standing": "nothing to add here",
    "bob-typing-desk-fire": "inbox on fire",
    "bob-typing-desk-fire-closeup": "inbox on fire, closer",
    "bob-fail": "something failed, epically",
}


def reactions_enabled() -> bool:
    """Kill switch: BOB_PROBE_REACTIONS=off disables the reaction tier
    (prompt stops offering clips; sends are refused even if a stale
    prompt produced one)."""
    return os.environ.get("BOB_PROBE_REACTIONS", "on").strip().lower() != "off"


def _reactions_block(bot_name: str) -> str:
    clips = "\n".join(f"- {name} — {cue}" for name, cue in REACTION_CLIPS.items())
    return f"""
Reactions: on STAND_DOWN you may additionally recommend one of {bot_name}'s reaction
clips (a short video of him reacting). Fire one ONLY when the moment genuinely hits —
a win someone announced, a glorious failure, real drama unfolding, chaos calmly
denied. A clip is for the rare perfect beat: NOT routine presence, NOT punctuation,
and when unsure recommend none — most STAND_DOWNs send no clip at all.
{clips}

Respond with ONLY a JSON object: {{"decision": "ACT"|"WAIT"|"STAND_DOWN", "react": <clip name from the list, or null>, "reason": "<brief>"}}"""  # noqa: E501


def probe_system_prompt(bot_name: str, *, reactions: bool = False) -> str:
    closing = (
        _reactions_block(bot_name) if reactions
        else '\nRespond with ONLY a JSON object: {"decision": "ACT"|"WAIT"|"STAND_DOWN", "reason": "<brief>"}'
    )
    return f"""\
You are an attention gate for a chatbot named "{bot_name}". A batch of group-chat
messages has arrived, none of which structurally addresses {bot_name} (no @mention,
no name, no reply to {bot_name}). Decide whether {bot_name} should respond.

Engagement rule (the core policy): {bot_name} speaks only when ENGAGED with the
conversation. He is engaged when:
- He is mid-exchange: the Engagement state line says he spoke recently (within the
  last few messages) AND the pending batch continues that same thread — a follow-up,
  a question about what he said, a callback to it.
- The batch asks something the group plainly expects {bot_name} specifically to
  answer — a factual query he is the natural answerer of, a request for help, a
  follow-up on a thread he started.
If neither holds, {bot_name} is a participant who was not spoken to and has nothing
at stake: reading along is the correct behaviour. Do not reply merely because he
*could* contribute.

Decisions:
- STAND_DOWN (the default):
  * Banter, jokes, or reactions between other people ("lol", "nice", "haha", emoji).
  * Messages grammatically addressed TO another specific person ("hey david", \
"sarah what's up?", "@tina ...") — the addressee is that person, not {bot_name}.
  * {bot_name} spoke earlier but the conversation has moved on — many messages since, \
new topic, engagement lapsed.
  * Pure social exchange between others with no question in it and nothing \
{bot_name} can answer or act on.
- ACT (respond now — the engagement rule clearly holds):
  * The batch continues an active exchange {bot_name} was part of in the last few \
turns — callbacks, references to a place/topic he just discussed.
  * {bot_name} was asked something in a recent message that hasn't been answered yet.
  * A question or request aimed at the GROUP that {bot_name} can answer or do \
something about — "does anyone know how to reset the router?", "what's the capital \
of Mongolia?", "can we find a pool session this weekend?". He is the group's \
assistant: an unaddressed general question or actionable request still engages him; \
it does not need to name him.
  * Names of third parties inside the message body ("tell David", "Audrey and Mabel \
like swimming") are topics, not addressees — when the message is otherwise a \
question/request aimed at the group, it is ACT-eligible.
- WAIT (defer, re-evaluate shortly):
  * The sender appears mid-thought (fragments, trailing "...", incomplete sentences).
  * Multiple people are actively deciding or discussing something and the thread \
hasn't settled — including a live exchange the group may soon turn into a question \
or request for {bot_name}. STAND_DOWN here only when the exchange clearly has \
nothing to do with him.

When ambiguous: STAND_DOWN. A person in a group who wasn't addressed and isn't in
the exchange stays quiet — {bot_name} errs the same way.

Transcript lines are labelled with the speaker's name — lines from {bot_name} itself are \
labelled "{bot_name}". Use these labels to tell participants apart and to spot who a \
message is addressed to. The Engagement state line is computed from the real history: \
trust it over your own impression of the transcript.
{closing}"""


async def probe_actionability(
    ctx: Any,
    session_key: str,
    *,
    bot_name: str = "Bob",
    model: str = "",
    max_context_messages: int = 10,
) -> dict:
    try:
        context_text = await _build_context(ctx, session_key, max_context_messages,
                                            bot_name=bot_name)
    except Exception:
        logger.warning("tier2: context build failed for %s, defaulting ACT",
                       session_key, exc_info=True)
        return {"decision": "ACT", "react": None, "reason": "context-build failure"}

    return await probe_decide(ctx, context_text, bot_name=bot_name, model=model,
                              session_key=session_key)


async def probe_decide(
    ctx: Any,
    context_text: str,
    *,
    bot_name: str = "Bob",
    model: str = "",
    session_key: str = "",
) -> dict:
    """Run the Tier 2 probe on an already-built context. Shared by the live
    path (probe_actionability) and the offline confusion-matrix eval
    (``bob replay probe-matrix``) so both score the exact same prompt,
    parameters, and parsing. Returns ``{"decision", "react", "reason"}``;
    ``react`` (a REACTION_CLIPS key) is set only on STAND_DOWN with the
    reaction tier enabled."""
    try:
        from server.services.llm_dispatch import LLMDispatchService

        result = await LLMDispatchService(ctx).chat(
            [{"role": "system", "content": probe_system_prompt(
                bot_name, reactions=reactions_enabled())},
             {"role": "user", "content": context_text}],
            model=model,
            temperature=0.0,
            # Headroom for low-effort reasoning — the verdict JSON is ~40
            # tokens, but thinking models spend reasoning from the same
            # budget (claim_router probe hit this at 60). Empty model
            # resolves through llm_dispatch to the configured default.
            max_tokens=300,
            reasoning_effort="low",
            call_category="attention_probe",
            session_key=session_key,
        )
    except Exception:
        # Fail closed-ish: WAIT defers the decision to the extension window
        # (one re-probe) rather than replying exactly when the LLM is flaky.
        # If the extension is already spent, the coordinator forces ACT.
        logger.warning("tier2: LLM call failed for %s, defaulting WAIT",
                       session_key, exc_info=True)
        return {"decision": "WAIT", "react": None, "reason": "probe LLM failure"}

    try:
        parsed = json.loads(result.strip())
        decision = str(parsed.get("decision", "ACT")).upper()
        reason = str(parsed.get("reason", "?"))
        if decision not in VALID:
            decision = "ACT"
        react = str(parsed.get("react") or "").strip().lower() or None
        if react is not None and react not in REACTION_CLIPS:
            logger.info("tier2: unknown reaction clip %r for %s — dropping", react, session_key)
            react = None
        if react is not None and (decision != "STAND_DOWN" or not reactions_enabled()):
            react = None  # a clip never rides ACT/WAIT or a disabled tier
        logger.info("tier2: %s for %s (reason: %s%s)",
                    decision, session_key, reason,
                    f", react: {react}" if react else "")
        return {"decision": decision, "react": react, "reason": reason}
    except (json.JSONDecodeError, AttributeError, ValueError, TypeError):
        upper = result.upper()
        for cand in ("STAND_DOWN", "WAIT", "ACT"):
            if cand in upper:
                logger.info("tier2: raw-parse %s for %s", cand, session_key)
                return {"decision": cand, "react": None, "reason": "raw-parse"}
        logger.warning("tier2: unparseable probe response for %s, defaulting ACT: %s",
                       session_key, result[:100])
        return {"decision": "ACT", "react": None, "reason": "unparseable"}


def _minutes_since(created_at: Any) -> float | None:
    if not created_at:
        return None
    try:
        ts = str(created_at).replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
    except ValueError:
        return None


async def _build_context(ctx: Any, session_key: str, max_context: int,
                         *, bot_name: str = "Bob") -> str:
    """Session agenda + engagement state + recent dispatched dialogue + the
    pending batch."""
    from server.repositories.history import HistoryRepository
    from server.repositories.participants import ParticipantRepository
    from server.services.session_agenda_service import SessionAgendaService

    parts: list[str] = []

    agenda = await SessionAgendaService(ctx).get_agenda(session_key)
    if agenda and agenda.strip():
        parts.append("## Session context")
        parts.append(agenda.strip())

    # Attribution: STAND_DOWN/WAIT heuristics key on WHO spoke ("hey david"
    # is a stand-down only if David didn't send it; one sender mid-thought
    # suggests WAIT), so resolve sender_id -> display name the same way
    # prompt assembly does.
    sender_names: dict[str, str] = {}
    for p in await ParticipantRepository(ctx.db).list_for(session_key):
        if p["contact_id"] and p["display_name"]:
            sender_names[p["contact_id"]] = p["display_name"]

    def speaker(row: dict) -> str:
        if row["role"] != "user":
            return bot_name
        return sender_names.get(row["sender_id"] or "") or "User"

    repo = HistoryRepository(ctx.db)
    rows = await repo.recent_dialogue(session_key, limit=max_context,
                                      dispatched_only=True)

    # Engagement state, computed not guessed: the ACT policy hinges on
    # "mid-exchange", so the transcript alone (which carries no timestamps)
    # must not be the model's only signal. NO_REPLY/empty assistant rows are
    # bookkeeping, not the bot speaking — same filter as the transcript.
    spoken = [i for i, row in enumerate(rows)
              if row["role"] == "assistant"
              and (row["content"] or "").strip()
              and not is_no_reply(row["content"])]
    parts.append("## Engagement state")
    if spoken:
        last = rows[spoken[-1]]
        msgs_since = len(rows) - 1 - spoken[-1]
        age_min = _minutes_since(last.get("created_at"))
        age = (f"about {max(0, int(round(age_min)))} minutes ago"
               if age_min is not None else "earlier in the recent window")
        parts.append(f"{bot_name} last spoke {msgs_since} message(s) ago ({age}).")
    else:
        parts.append(f"{bot_name} has not spoken in the recent conversation — not engaged.")

    if rows:
        parts.append("## Recent conversation")
        for row in rows:
            # Routine prompts and subagent chatter ride the same history but
            # aren't dialogue by group participants — without this they render
            # as anonymous "User:" lines and pollute the transcript the probe
            # reads. Routine OUTPUTS (assistant rows) stay: they're real
            # messages the bot sent and humans may be replying to.
            if row["role"] == "user" and row["channel"] in ("routine", "subagent"):
                continue
            # Same stale-marker skip as prompt assembly: NO_REPLY/empty rows
            # are internal bookkeeping, not replies, and read to the probe
            # like the bot already declined.
            if row["role"] == "assistant" and (
                    not (row["content"] or "").strip()
                    or is_no_reply(row["content"])):
                continue
            parts.append(f"{speaker(row)}: {(row['content'] or '')[:200]}")

    pending = await repo.recent_dialogue(session_key, limit=10,
                                         dispatched_only=False,
                                         pending_only=True)
    if pending:
        parts.append("## Pending unprocessed messages")
        for row in pending:
            parts.append(f"{speaker(row)}: {(row['content'] or '')[:300]}")

    parts.append("## Decision")
    parts.append(f"Should {bot_name} respond to the pending batch? "
                 "Reply with the JSON decision object.")
    return "\n".join(parts)
