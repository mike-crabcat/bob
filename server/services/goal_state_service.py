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

from server.context import AppContext

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
