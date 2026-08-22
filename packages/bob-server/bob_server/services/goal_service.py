"""Goal service (Bob3 Phase V).

Lifecycle glue above GoalRepository: goal mutations run as effects (durable,
idempotent); completing or failing a goal cancels its outstanding wakeups,
appends a goal event, and wakes the origin conversation with the result.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from bob_server.context import AppContext
from bob_server.repositories.event_log import Event, EventLogRepository
from bob_server.repositories.goals import GoalRepository
from bob_server.repositories.wakeups import WakeupRepository

logger = logging.getLogger(__name__)


async def create_goal(
    ctx: AppContext,
    *,
    conversation_id: str,
    objective: str,
    origin_conversation_id: str | None = None,
    kind: str = "task",
    strategy: dict[str, Any] | None = None,
    deadline: str | None = None,
    external_ref: str | None = None,
    goal_id: str | None = None,
) -> dict[str, Any]:
    """Create an active goal; a deadline schedules a wakeup for the origin
    (or working) conversation so unanswered goals resurface."""
    repo = GoalRepository(ctx.db)
    goal = await repo.create(
        conversation_id=conversation_id,
        objective=objective,
        origin_conversation_id=origin_conversation_id,
        kind=kind,
        strategy_json=json.dumps(strategy) if strategy else None,
        deadline=deadline,
        external_ref=external_ref,
        goal_id=goal_id,
    )
    if deadline:
        await WakeupRepository(ctx.db).schedule(
            conversation_id=origin_conversation_id or conversation_id,
            not_before=deadline,
            goal_id=goal["id"],
        )
    await _append_goal_event(ctx, goal, "goal.created")
    return goal


async def settle_goal(
    ctx: AppContext,
    goal_id: str,
    *,
    status: str,
    result: str,
    wake_origin: bool = True,
    note: str | None = None,
) -> bool:
    """Terminal transition (completed/failed/cancelled). Exactly one settler
    wins the CAS; the winner cancels wakeups, appends the goal event, and
    wakes the origin conversation with the result in context."""
    repo = GoalRepository(ctx.db)
    moved = await repo.transition(goal_id, to_status=status, result=result, note=note)
    if not moved:
        return False

    await WakeupRepository(ctx.db).cancel_for_goal(goal_id)
    goal = await repo.get(goal_id)
    if goal is None:
        return True
    await _append_goal_event(ctx, goal, f"goal.{status}")

    origin = goal["origin_conversation_id"]
    if wake_origin and origin and origin != goal["conversation_id"]:
        from bob_server.services.wake_service import wake_conversation

        content = (
            f"## Goal {status}\n"
            f"Objective: {goal['objective']}\n\n"
            f"{result}"
        )
        try:
            await wake_conversation(
                ctx, origin, content,
                call_category="goal_result",
                metadata={"goal_id": goal_id, "goal_kind": goal["kind"]},
            )
        except Exception:
            logger.exception("goal %s: failed to wake origin %s", goal_id, origin)
    return True


async def complete_goal(ctx: AppContext, goal_id: str, *, result: str,
                        wake_origin: bool = True) -> bool:
    return await settle_goal(ctx, goal_id, status="completed", result=result,
                             wake_origin=wake_origin)


async def fail_goal(ctx: AppContext, goal_id: str, *, error: str,
                    wake_origin: bool = True) -> bool:
    return await settle_goal(ctx, goal_id, status="failed", result=error,
                             wake_origin=wake_origin)


async def _append_goal_event(ctx: AppContext, goal: dict[str, Any], event_type: str) -> None:
    try:
        await EventLogRepository(ctx.db).append(Event(
            event_type=event_type,
            binding_key=f"goal:{goal['id']}",
            conversation_id=goal["conversation_id"],
            source="goals",
            external_id=f"{goal['id']}:{event_type}:{goal['version']}",
            payload={
                "goal_id": goal["id"],
                "kind": goal["kind"],
                "objective": goal["objective"],
                "status": goal["status"],
                "origin_conversation_id": goal["origin_conversation_id"],
                "result": goal["result"],
            },
        ))
    except Exception:
        logger.warning("failed to append %s for goal %s", event_type, goal["id"],
                       exc_info=True)


async def fire_wakeup(ctx: AppContext, wakeup: dict[str, Any]) -> None:
    """Deliver a claimed wakeup: wake its conversation with goal context (if
    the linked goal is still active) or a plain scheduled-wake notice."""
    from bob_server.services.wake_service import wake_conversation

    goal = None
    if wakeup["goal_id"]:
        goal = await GoalRepository(ctx.db).get(wakeup["goal_id"])
        if goal and goal["status"] != "active":
            return  # goal already settled; wakeup is moot

    if goal:
        content = (
            f"## Goal deadline reached\n"
            f"Objective: {goal['objective']}\n"
            f"Status: still active (no result yet)\n"
            f"Progress: {goal['progress'] or 'none recorded'}\n\n"
            f"Decide how to proceed: follow up, revise the goal, or report back."
        )
        category = "goal_deadline"
    else:
        content = "## Scheduled wakeup\nA scheduled wakeup for this conversation fired."
        category = "wakeup"

    await wake_conversation(
        ctx, wakeup["conversation_id"], content,
        call_category=category,
        metadata={"wakeup_id": wakeup["id"], "goal_id": wakeup["goal_id"]},
    )


async def pump_due_wakeups(ctx: AppContext, *, limit: int = 20) -> int:
    """Claim and deliver due wakeups. Recurrence: after firing, a recurrence
    spec reschedules the next occurrence (simple '+<minutes>m' interval v1)."""
    repo = WakeupRepository(ctx.db)
    claimed = await repo.claim_due(limit=limit)
    for wakeup in claimed:
        try:
            await fire_wakeup(ctx, wakeup)
        except Exception:
            logger.exception("wakeup %s delivery failed", wakeup["id"])
        rec = wakeup["recurrence"]
        if rec and rec.startswith("+") and rec.endswith("m"):
            try:
                minutes = int(rec[1:-1])
                next_at = (datetime.now(timezone.utc)
                           + timedelta(minutes=minutes)).isoformat()
                await repo.schedule(
                    conversation_id=wakeup["conversation_id"],
                    not_before=next_at,
                    goal_id=wakeup["goal_id"],
                    recurrence=rec,
                    tz=wakeup["tz"],
                )
            except (ValueError, TypeError):
                logger.warning("wakeup %s: bad recurrence %r", wakeup["id"], rec)
    return len(claimed)
