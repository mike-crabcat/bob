"""Goal state service — the structured-reasoning reviser (bob-events-plan.md §1.3).

Every goal carries a living worksheet in ``strategy_json`` (v2 schema: plan,
known, open_questions, next_actions, refs) so state is never re-derived from
transcripts. This module owns that schema and the cheap-model reviser that
folds new information into it:

- ``revise_goal_state`` — read goal → cheap-model fold → CAS write → optional
  wake of the goal's working conversation when the reviser judges the change
  warrants a turn. Silent updates are the default.
- ``enqueue_revision`` — the durable effect wrapper (kind ``goal_revise_state``,
  idempotency key ``goal_revise:{goal_id}:{stimulus_id}``).

Hard rule (plan §1.3): the reviser NEVER actuates. It updates state and may
ask for a wake; creating goals, placing calls, sending messages, and
scheduling wakeups all belong to the woken main model with its full tool
surface and judgment.

Failure philosophy: degrade to "tell the main model" rather than lose
information — malformed output and CAS exhaustion both keep the old state and
wake instead of dropping the stimulus.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bob_server.context import AppContext

logger = logging.getLogger(__name__)

STRATEGY_VERSION = 2


class NextAction(BaseModel):
    model_config = ConfigDict(extra="ignore")
    action: str
    due: str = ""


class StrategyRefs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    entities: list[str] = Field(default_factory=list)
    claims: list[str] = Field(default_factory=list)


class GoalStrategy(BaseModel):
    """v2 strategy envelope. Unknown keys are preserved (``extra="allow"``) so
    later revisions (decision rules, pending_order, …) round-trip."""
    model_config = ConfigDict(extra="allow")
    v: int = STRATEGY_VERSION
    plan: str = ""
    known: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    next_actions: list[NextAction] = Field(default_factory=list)
    refs: StrategyRefs = Field(default_factory=StrategyRefs)
    legacy_outreach: dict[str, Any] | None = None


def parse_strategy(goal: dict[str, Any]) -> GoalStrategy:
    """Parse a goal row's strategy_json into the v2 envelope.

    Legacy shapes (no ``v`` — today only outreach's ``{requestor, message}``)
    are wrapped under ``legacy_outreach`` on first touch; goals that are never
    revised are never rewritten."""
    try:
        raw = json.loads(goal.get("strategy_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    if raw.get("v") == STRATEGY_VERSION:
        try:
            return GoalStrategy.model_validate(raw)
        except ValidationError:
            pass
    return GoalStrategy(legacy_outreach=raw or None)


def strategy_json_for(state: GoalStrategy) -> str:
    return json.dumps(state.model_dump(mode="json", exclude_none=True))


def render_strategy(state: GoalStrategy, *, max_items: int = 5) -> str:
    """Compact human/LLM-readable rendering for prompts (plan §1.4 budget)."""
    lines: list[str] = []
    if state.plan:
        lines.append(f"Plan: {state.plan[:240]}")
    for item in state.known[:max_items]:
        lines.append(f"Known: {item[:200]}")
    for item in state.open_questions[:max_items]:
        lines.append(f"Open: {item[:200]}")
    for na in state.next_actions[:max_items]:
        lines.append(f"Next: {na.action[:200]}" + (f" (due {na.due})" if na.due else ""))
    if state.refs.entities:
        lines.append(f"Entities: {', '.join(state.refs.entities[:8])}")
    if state.legacy_outreach:
        lo = state.legacy_outreach
        if lo.get("requestor"):
            lines.append(f"Requested by: {lo['requestor']}")
        if lo.get("message"):
            lines.append(f"Initial message: \"{str(lo['message'])[:200]}\"")
    return "\n".join(lines)


def _shadow_mode() -> bool:
    return os.getenv("BOB_GOAL_STATE_SHADOW", "").strip().lower() in ("1", "true", "yes", "on")


# Per-goal serialization (plan §2.2): revisions of the same goal run one at a
# time so CAS conflicts come from genuinely concurrent writers, not from our
# own fan-out. asyncio primitives bind to the running loop, so keys carry the
# loop identity — one loop per process in production, but tests get a fresh
# loop each case.
_GOAL_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}

# Global cap on concurrent reviser LLM calls (lazily sized from settings on
# first use — module import happens before env-driven Settings exist).
_REVISER_SEMAPHORES: dict[int, asyncio.Semaphore] = {}


async def _semaphore(ctx: AppContext) -> asyncio.Semaphore:
    loop_id = id(asyncio.get_running_loop())
    sem = _REVISER_SEMAPHORES.get(loop_id)
    if sem is None:
        sem = _REVISER_SEMAPHORES[loop_id] = asyncio.Semaphore(
            ctx.settings.goals.max_concurrent_revisions)
    return sem


def _lock_for(goal_id: str) -> asyncio.Lock:
    key = (id(asyncio.get_running_loop()), goal_id)
    lock = _GOAL_LOCKS.get(key)
    if lock is None:
        lock = _GOAL_LOCKS[key] = asyncio.Lock()
    return lock


def _reviser_system_prompt() -> str:
    return """\
You are the goal-state reviser for an AI assistant. You maintain one goal's
living worksheet: what is planned, what is known, what is still open, and the
next actions. You are given the goal, its current state, and a stimulus (new
information — a child goal's result, newly extracted memory claims, a
deadline nudge, or a coherence review).

Rules:
- Fold the stimulus into the state. Update `known`/`open_questions` to reflect
  it; adjust `plan` and `next_actions` only when they actually change.
- When the stimulus contains conflicting facts (e.g. an old "attending" and a
  new "can't make it" for the same person), newest wins: record the newest
  fact and drop or supersede the stale one.
- Facts that came from a private DM (the stimulus says so) must be marked in
  `known` with a "private_source:" prefix — the main model must not repeat
  them into group messages.
- NEVER invent facts not present in the goal or stimulus.
- You do not act: no messages, calls, goals, or wakeups. If action is needed,
  add it to `next_actions` and set wake_needed=true.
- Set wake_needed=true ONLY if the stimulus changes decisions or next actions
  (new answer, conflict resolved, deadline forced a choice, action now
  overdue). Routine confirmations of what is already known → wake_needed=false.
- Decision rules: if the state carries a `decision` rule (e.g.
  {"quorum": 0.75, "of": "invitees", "decide_by": "<iso>"}), evaluate it
  against the accumulated `known` on every relevant stimulus. When a rule is
  satisfied — or decide_by has passed — set next_actions to decide/settle
  (e.g. "settle this goal: quorum reached", "decide now with what you have")
  and wake_needed=true. Do not settle anything yourself; the woken model does.
- Time-bound goals: when a concrete date-time is agreed (meetup, launch,
  call), encode follow-through as next_actions with explicit ISO `due`
  timestamps INCLUDING their UTC offset (e.g. the reminder due the evening
  before, recording the outcome due just after) — not prose like "before the
  meetup". A scheduler wakes the assistant when a due enters its window, so
  only a real timestamp triggers follow-through; prose dues never fire. When
  the event instant differs from the goal's deadline, note in `known` that
  the deadline should be the event time (the main model owns setting it).

Respond with ONLY a JSON object:
{"state": {"v": 2, "plan": "...", "known": ["..."], "open_questions": ["..."],
 "next_actions": [{"action": "...", "due": "ISO or empty"}],
 "refs": {"entities": ["..."], "claims": ["..."]}},
 "wake_needed": false, "wake_summary": "one sentence when wake_needed"}"""


def _extract_json(text: str) -> dict[str, Any]:
    """Parse the reviser's JSON object, tolerating fences and prose padding."""
    text = text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        parsed = json.loads(text[start:end + 1])
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("no JSON object in reviser response")


async def _call_reviser(
    ctx: AppContext, goal: dict[str, Any], state: GoalStrategy, stimulus: str,
) -> tuple[GoalStrategy, bool, str]:
    """One reviser LLM pass with a single validation retry. Raises on hard
    failure; callers degrade to wake."""
    from bob_server.services.llm_dispatch import LLMDispatchService

    model = (ctx.settings.goals.reviser_model
             or ctx.settings.openai.get_memory_model())
    state_json = json.dumps(json.loads(strategy_json_for(state)), indent=1)
    user = (
        f"# Goal ({goal['kind']})\n{goal['objective']}\n\n"
        f"# Current state\n{state_json}\n\n"
        f"# Stimulus\n{stimulus}\n\n"
        "Return the updated state JSON object per the contract."
    )
    from bob_server.services.prompt_assembler import local_now_prompt_line

    # Stamp-only clock (tools_hint=False — this call carries no tools): the
    # reviser reasons about ISO dues and decide_by windows, so it needs a
    # grounded now like any turn (2026-09-01 time-grounding fan-out).
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _reviser_system_prompt() + "\n\n" + local_now_prompt_line(tools_hint=False)},
        {"role": "user", "content": user},
    ]

    async with await _semaphore(ctx):
        last_error = ""
        for attempt in (1, 2):
            result = await LLMDispatchService(ctx).chat(
                messages,
                model=model,
                temperature=0.0,
                # Must cover reasoning + content: thinking models share one
                # output budget, and a 900 cap returned empty text (all
                # reasoning) on GLM-5.3-flash.
                max_tokens=ctx.settings.goals.reviser_max_tokens,
                reasoning_effort="low",
                call_category="goal_revise",
                session_key=goal["conversation_id"],
            )
            try:
                parsed = _extract_json(result)
                new_state = GoalStrategy.model_validate(parsed.get("state") or {})
                wake_needed = bool(parsed.get("wake_needed"))
                summary = str(parsed.get("wake_summary") or "")[:400]
                return new_state, wake_needed, summary
            except (ValueError, json.JSONDecodeError, ValidationError) as exc:
                last_error = str(exc)[:300]
                logger.warning("goal %s: reviser output invalid (attempt %d): %s",
                               goal["id"], attempt, last_error)
                messages = messages + [
                    {"role": "assistant", "content": result[:2000]},
                    {"role": "user", "content":
                     f"Your output was invalid ({last_error}). Respond again with "
                     "ONLY the JSON object per the contract, corrected."},
                ]
    raise ValueError(f"reviser output invalid after retry: {last_error}")


async def revise_goal_state(
    ctx: AppContext,
    goal_id: str,
    stimulus: str,
    *,
    stimulus_id: str,
    allow_wake: bool = True,
) -> dict[str, Any]:
    """Fold ``stimulus`` into the goal's strategy via the cheap-model reviser,
    CAS-write it, and wake the goal's working conversation when warranted.

    Returns an outcome dict for logging: {"outcome": revised|no_change|
    skipped|error, "wake": wake|no_wake|shadow_wake, "goal_id", "stimulus_id"}.
    """
    from bob_server.repositories.goals import GoalRepository

    outcome = {"goal_id": goal_id, "stimulus_id": stimulus_id,
               "outcome": "error", "wake": "no_wake"}
    repo = GoalRepository(ctx.db)
    async with _lock_for(goal_id):
        goal = await repo.get(goal_id)
        if goal is None or goal["status"] != "active":
            outcome["outcome"] = "skipped"
            return outcome
        state = parse_strategy(goal)

        try:
            new_state, wake_needed, summary = await _call_reviser(
                ctx, goal, state, stimulus)
        except Exception as exc:
            # Degrade: keep old state, wake the working conversation with the
            # raw stimulus so the main model folds it manually.
            logger.warning("goal %s: reviser failed (%s); degrading to wake",
                           goal_id, exc)
            return await _degrade_to_wake(ctx, goal, stimulus, outcome,
                                          allow_wake=allow_wake,
                                          detail=f"reviser error: {exc}")

        # CAS write with re-read retries (plan §1.3).
        max_retries = ctx.settings.goals.max_cas_retries
        wrote = False
        changed = True
        current = goal
        for _ in range(max_retries):
            new_json = strategy_json_for(new_state)
            if current["strategy_json"] == new_json:
                changed = False  # reviser folded nothing new; no write needed
                wrote = True
                break
            if await repo.revise(current["id"], expected_version=current["version"],
                                 strategy_json=new_json):
                wrote = True
                break
            current = await repo.get(goal_id)
            if current is None or current["status"] != "active":
                outcome["outcome"] = "skipped"
                return outcome
        if not wrote:
            logger.warning("goal %s: revise CAS exhausted; degrading to wake", goal_id)
            return await _degrade_to_wake(ctx, goal, stimulus, outcome,
                                          allow_wake=allow_wake,
                                          detail="cas exhausted")

        outcome["outcome"] = "revised" if changed else "no_change"
        if not wake_needed or not allow_wake:
            outcome["wake"] = "no_wake"
            return outcome

        content = (
            f"## Goal progress\n"
            f"Objective: {goal['objective']}\n\n"
            f"{summary or stimulus[:600]}\n\n"
            f"Updated state:\n{render_strategy(new_state) or '(none recorded)'}"
        )
        return await _wake_working(ctx, goal, content, outcome)


async def _degrade_to_wake(
    ctx: AppContext, goal: dict[str, Any], stimulus: str, outcome: dict[str, Any],
    *, allow_wake: bool, detail: str,
) -> dict[str, Any]:
    outcome["outcome"] = "error"
    outcome["detail"] = detail[:500]
    if not allow_wake:
        outcome["wake"] = "no_wake"
        return outcome
    content = (
        f"## Goal progress (state reviser unavailable)\n"
        f"Objective: {goal['objective']}\n\n"
        f"New information that could not be folded automatically:\n{stimulus[:800]}\n\n"
        "Fold it into the goal state yourself (update_goal_state)."
    )
    return await _wake_working(ctx, goal, content, outcome)


async def _wake_working(
    ctx: AppContext, goal: dict[str, Any], content: str, outcome: dict[str, Any],
) -> dict[str, Any]:
    """Wake the goal's working conversation (plan §1.2 wake matrix — reviser
    wakes only the working conversation, never the origin). Shadow mode
    records the would-be wake instead."""
    if _shadow_mode():
        outcome["wake"] = "shadow_wake"
        logger.info("goal %s: shadow-mode suppressing wake (stimulus %s)",
                    goal["id"], outcome.get("stimulus_id"))
        return outcome
    from bob_server.services.wake_service import wake_conversation

    try:
        await wake_conversation(
            ctx, goal["conversation_id"], content,
            call_category="goal_progress",
            metadata={"goal_id": goal["id"], "stimulus_id": outcome.get("stimulus_id")},
        )
        outcome["wake"] = "wake"
    except Exception:
        logger.exception("goal %s: progress wake failed", goal["id"])
        outcome["wake"] = "no_wake"
        outcome["outcome"] = "error"
    return outcome


def _register_reviser_executor() -> None:
    from bob_server.services import effects as effects_svc

    async def _exec(ctx, payload):
        result = await revise_goal_state(
            ctx, payload["goal_id"], payload["stimulus"],
            stimulus_id=payload["stimulus_id"],
            allow_wake=payload.get("allow_wake", True),
        )
        return f"{result.get('outcome')}:{result.get('wake')}"

    effects_svc.register_executor("goal_revise_state", _exec, retryable=True)


_register_reviser_executor()


async def enqueue_revision(
    ctx: AppContext,
    goal_id: str,
    stimulus: str,
    *,
    stimulus_id: str,
    allow_wake: bool = True,
    inline: bool = True,
) -> dict[str, Any]:
    """Durably enqueue a reviser run (effect kind ``goal_revise_state``).
    Idempotent per (goal, stimulus): replays and double-enqueues are
    suppressed by the effects' unique idempotency key.

    ``inline=False`` records the effect for the background pump instead of
    delivering in this call stack — used by the settle roll-up, which
    typically already runs inside an effect executor; nesting a reviser LLM
    call (and its own wake dispatch) inside that chain re-enters the
    connection pool and the per-goal locks for no benefit."""
    payload = {"goal_id": goal_id, "stimulus": stimulus,
               "stimulus_id": stimulus_id, "allow_wake": allow_wake}
    key = f"goal_revise:{goal_id}:{stimulus_id}"
    if not inline:
        from bob_server.repositories.effects import EffectRepository
        effect_id = await EffectRepository(ctx.db).emit(
            kind="goal_revise_state", idempotency_key=key, payload=payload)
        return {"ok": True, "effect_id": effect_id, "queued": True}

    from bob_server.services.effects import emit_and_deliver

    return await emit_and_deliver(
        ctx, kind="goal_revise_state", idempotency_key=key, payload=payload)
