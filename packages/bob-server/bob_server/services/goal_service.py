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
    parent_goal_id: str | None = None,
    goal_id: str | None = None,
) -> dict[str, Any]:
    """Create an active goal; a deadline schedules a wakeup so unanswered
    goals resurface.

    Bob Events §1.1/§1.2: ids are canonicalised via ``resolve_cid``; the goal
    registers its working conversation (worker) and origin (origin) as
    holders; a child goal's deadline wakeup targets the ROOT goal's working
    conversation, never the child's own channel endpoint."""
    from bob_server.repositories.conversations import ConversationRepository

    repo = GoalRepository(ctx.db)
    conv_repo = ConversationRepository(ctx.db)
    cid = await conv_repo.resolve_cid(conversation_id)
    origin_cid = (await conv_repo.resolve_cid(origin_conversation_id)
                  if origin_conversation_id else None)

    goal = await repo.create(
        conversation_id=cid,
        objective=objective,
        origin_conversation_id=origin_cid,
        kind=kind,
        strategy_json=json.dumps(strategy) if strategy else None,
        deadline=deadline,
        external_ref=external_ref,
        parent_goal_id=parent_goal_id,
        goal_id=goal_id,
    )
    await repo.add_holder(goal["id"], cid, role="worker")
    if origin_cid and origin_cid != cid:
        await repo.add_holder(goal["id"], origin_cid, role="origin")

    if deadline:
        await WakeupRepository(ctx.db).schedule(
            conversation_id=await _wakeup_target(repo, goal),
            not_before=deadline,
            goal_id=goal["id"],
        )
    await _append_goal_event(ctx, goal, "goal.created")
    return goal


async def _wakeup_target(repo: GoalRepository, goal: dict[str, Any]) -> str:
    """Where this goal's deadline wakeups land (plan §1.2 wake matrix): a
    child's deadline wakes the ROOT's working conversation (never an outreach
    child's target DM); a root goal keeps today's behaviour — the origin
    (asker) if set, else its own working conversation."""
    if goal.get("parent_goal_id"):
        root = await repo.root_of(goal["id"])
        if root is not None:
            return root["conversation_id"]
    return goal["origin_conversation_id"] or goal["conversation_id"]


async def settle_goal(
    ctx: AppContext,
    goal_id: str,
    *,
    status: str,
    result: str,
    wake_origin: bool = True,
    note: str | None = None,
    wake_content: str | None = None,
    wake_category: str | None = None,
) -> bool:
    """Terminal transition (completed/failed/cancelled). Exactly one settler
    wins the CAS; the winner cancels wakeups, appends the goal event, and
    wakes the origin conversation with the result in context.

    Bob Events §1.2 wake matrix — the single chokepoint every settle caller
    inherits:
    - child goal (has parent): NEVER wakes the origin directly. The result
      rolls up as a reviser stimulus on the parent; the reviser decides
      whether the parent's working conversation gets a ``goal_progress`` wake.
    - root goal: wakes the origin (today's behaviour), with optional caller
      overrides for content/category (e.g. call results).
    """
    repo = GoalRepository(ctx.db)
    moved = await repo.transition(goal_id, to_status=status, result=result, note=note)
    if not moved:
        return False

    await WakeupRepository(ctx.db).cancel_for_goal(goal_id)
    goal = await repo.get(goal_id)
    if goal is None:
        return True
    await _append_goal_event(ctx, goal, f"goal.{status}")

    parent_id = goal.get("parent_goal_id")
    if parent_id:
        await _roll_up_to_parent(ctx, goal, status, result)
        return True

    origin = goal["origin_conversation_id"]
    if wake_origin and origin and origin != goal["conversation_id"]:
        from bob_server.services.wake_service import wake_conversation

        content = wake_content or (
            f"## Goal {status}\n"
            f"Objective: {goal['objective']}\n\n"
            f"{result}"
        )
        try:
            await wake_conversation(
                ctx, origin, content,
                call_category=wake_category or "goal_result",
                metadata={"goal_id": goal_id, "goal_kind": goal["kind"]},
            )
        except Exception:
            logger.exception("goal %s: failed to wake origin %s", goal_id, origin)
    return True


async def _roll_up_to_parent(
    ctx: AppContext, child: dict[str, Any], status: str, result: str,
) -> None:
    """Child-settle roll-up (plan §1.2): enqueue a durable reviser run on the
    parent with the child's outcome as the stimulus. If effect enqueueing
    itself fails, degrade to a direct wake of the parent's working
    conversation — information must not be lost to infrastructure."""
    from bob_server.services.goal_state_service import enqueue_revision

    stimulus = (
        f"## Child goal {status}\n"
        f"Objective: {child['objective']}\n\n"
        f"Result: {result}"
    )
    try:
        await enqueue_revision(
            ctx, child["parent_goal_id"], stimulus,
            stimulus_id=f"settle:{child['id']}:{status}",
        )
    except Exception:
        logger.exception("goal %s: roll-up enqueue failed; degrading to direct wake",
                         child["id"])
        from bob_server.services.wake_service import wake_conversation

        parent = await GoalRepository(ctx.db).get(child["parent_goal_id"])
        if parent is None:
            return
        try:
            await wake_conversation(
                ctx, parent["conversation_id"], stimulus,
                call_category="goal_progress",
                metadata={"goal_id": parent["id"],
                          "rolled_up_from": child["id"]},
            )
        except Exception:
            logger.exception("goal %s: roll-up degrade wake failed", child["id"])


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


async def fire_wakeup(ctx: AppContext, wakeup: dict[str, Any]) -> bool:
    """Deliver a claimed wakeup. Returns True if a recurrence (when present)
    should be rescheduled, False to let the series lapse.

    Kinds:
      routine — look up the routine definition and dispatch it (detached, so
                a slow LLM run never blocks the pump). Deleted/disabled
                routines end their series; a validity-window miss skips the
                run but keeps the series alive.
      wake    — goal-deadline or plain scheduled wake for a conversation.
    """
    from bob_server.services.wake_service import wake_conversation

    if wakeup.get("kind") == "routine":
        from bob_server.services import routine_service as routines

        payload = json.loads(wakeup.get("payload_json") or "{}")
        routine = await routines.RoutineService(ctx).get_by_id(
            payload.get("routine_id", ""))
        if not routine or not routine["enabled"]:
            return False  # definition gone/disabled: series ends
        if routines._outside_validity_window(routine):
            return True   # skip this run, keep the schedule
        await routines.append_fired_event(ctx, routine, wakeup["not_before"])
        routines.fire_routine_detached(ctx, routine)
        return True

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
    return True


def _next_occurrence(wakeup: dict[str, Any]) -> str | None:
    """Compute the next not_before for a recurring wakeup, or None.

    Specs: '+<minutes>m' simple interval, or 'cron:<expr>' interpreted in the
    wakeup's tz. Always stored as UTC ISO so claim_due's TEXT compare is safe
    (the routines continuous-fire regression, now guarded at this seam).
    """
    rec = wakeup.get("recurrence")
    if not rec:
        return None
    try:
        if rec.startswith("+") and rec.endswith("m"):
            minutes = int(rec[1:-1])
            return (datetime.now(timezone.utc)
                    + timedelta(minutes=minutes)).isoformat()
        if rec.startswith("cron:"):
            from bob_server.cron import next_cron_occurrence
            occurrence = next_cron_occurrence(rec[5:], timezone=wakeup.get("tz"))
            return occurrence.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        pass
    logger.warning("wakeup %s: bad recurrence %r", wakeup.get("id"), rec)
    return None


async def pump_due_wakeups(ctx: AppContext, *, limit: int = 20) -> int:
    """Claim and deliver due wakeups. Recurrence ('+<minutes>m' or
    'cron:<expr>') reschedules the next occurrence after firing — claim-first,
    so a slow delivery can never double-fire a slot."""
    repo = WakeupRepository(ctx.db)
    claimed = await repo.claim_due(limit=limit)
    for wakeup in claimed:
        reschedule = True
        try:
            reschedule = await fire_wakeup(ctx, wakeup)
        except Exception:
            logger.exception("wakeup %s delivery failed", wakeup["id"])
        if not reschedule:
            continue
        next_at = _next_occurrence(wakeup)
        if next_at:
            payload = json.loads(wakeup.get("payload_json") or "{}")
            await repo.schedule(
                conversation_id=wakeup["conversation_id"],
                not_before=next_at,
                goal_id=wakeup["goal_id"],
                recurrence=wakeup["recurrence"],
                tz=wakeup["tz"],
                kind=wakeup.get("kind") or "wake",
                payload=payload,
            )
    return len(claimed)
