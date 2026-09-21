"""Goal service (Bob3 Phase V).

Lifecycle glue above GoalRepository: goal mutations run as effects (durable,
idempotent); completing or failing a goal cancels its outstanding wakeups,
appends a goal event, and wakes the origin conversation with the result.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from server.context import AppContext
from server.repositories.event_log import Event, EventLogRepository
from server.repositories.goals import GoalRepository
from server.repositories.wakeups import WakeupRepository

logger = logging.getLogger(__name__)

# Lenient due extraction: reviser/model-written dues carry prose padding
# ("Before 2026-09-01T10:00:00+08:00" seen live) around an ISO instant.
_DUE_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?")


def extract_due_instant(due: str | None) -> datetime | None:
    """Pull a timezone-aware instant out of a next_action ``due`` string.
    Naive timestamps are read as UTC (the reviser contract asks for ISO with
    offset; prose like 'tomorrow' returns None — no false triggers)."""
    if not due:
        return None
    match = _DUE_PATTERN.search(str(due))
    if not match:
        return None
    raw = match.group(0).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


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
    from server.repositories.conversations import ConversationRepository

    repo = GoalRepository(ctx.db)
    conv_repo = ConversationRepository(ctx.db)
    cid = await conv_repo.resolve_cid(conversation_id)
    origin_cid = (await conv_repo.resolve_cid(origin_conversation_id)
                  if origin_conversation_id else None)

    # Goal rooms (docs/goal-rooms-plan.md): qualifying kinds get their own
    # utility conversation as the working surface — the charter carries the
    # objective + standing rules, subscriptions seed from refs/mentions, and
    # a check-in wakeup series is created with the room (D13: no room
    # without a next check-in). The asking conversation becomes the origin.
    from server.services import goal_rooms
    use_room = goal_rooms.kind_gets_room(ctx, kind)
    gid = goal_id or str(uuid4())
    if use_room:
        await goal_rooms.ensure_room(
            ctx, goal_id=gid, objective=objective, kind=kind,
            deadline=deadline, origin_session=cid)
        cid = goal_rooms.room_session_key(gid)
        if origin_cid is None:
            origin_cid = await conv_repo.resolve_cid(conversation_id)

    goal = await repo.create(
        conversation_id=cid,
        objective=objective,
        origin_conversation_id=origin_cid,
        kind=kind,
        strategy_json=json.dumps(strategy) if strategy else None,
        deadline=deadline,
        external_ref=external_ref,
        parent_goal_id=parent_goal_id,
        goal_id=gid,
    )
    await repo.add_holder(goal["id"], cid, role="worker")
    if origin_cid and origin_cid != cid:
        await repo.add_holder(goal["id"], origin_cid, role="origin")

    if use_room:
        # Seed the attention set (D8) and the pulse. With the goal loop
        # enabled (docs/goal-execution-plan.md) the pulse is the
        # continuation contract + dead-man heartbeat; without it, the
        # legacy fixed check-in series. The deadline wakeup below still
        # lands via _wakeup_target — for a room goal that IS the room
        # (deadlines follow brains, plan D10).
        await goal_rooms.seed_subscriptions(
            ctx, room_key=cid, origin_session=origin_cid or "",
            strategy=strategy)
        from server.services import goal_loop
        if goal_loop.loop_enabled(ctx):
            await goal_loop.ensure_loop(ctx, goal)
        else:
            await goal_rooms.schedule_checkin(ctx, goal)

    if deadline:
        await WakeupRepository(ctx.db).schedule(
            conversation_id=await _wakeup_target(repo, goal),
            not_before=deadline,
            goal_id=goal["id"],
        )
    await _append_goal_event(ctx, goal, "goal.created")
    return goal


async def _wakeup_target(repo: GoalRepository, goal: dict[str, Any]) -> str:
    """Where this goal's deadline/reminder wakeups land (wake matrix,
    revised 2026-09-16): always the conversation WORKING the goal — a
    child resolves to its root's working conversation — because the
    follow-up ladder and send tools live there. The origin (asker) is
    woken on completion instead (settle_goal owns that). The 2026-09-14
    WFH-roster incident: five root-goal deadline wakes landed in the
    origin group, producing one public status check instead of the
    authorised per-contact DM escalation."""
    if goal.get("parent_goal_id"):
        root = await repo.root_of(goal["id"])
        if root is not None:
            return root["conversation_id"]
    return goal["conversation_id"]


# Completed-goal error signatures (2026-09-10 incident): a Meshy submit
# returned HTTP 400 insufficientCredits, the script exited 0 anyway, the
# goal closed as completed — and the completion wake was narrated as "On
# it — testing" because the error sat at the bottom of the result. Any of
# these in a COMPLETED goal's result means the job probably did not
# succeed; the wake must say so up top.
_RESULT_ERROR_SIGNATURES = (
    "insufficientcredits", "insufficient credits",
    "http 400", "http 401", "http 402", "http 403", "http 404",
    "http 422", "http 429", "http 5",
    '"errors": [', '"errors":[',
    "traceback (most recent call last)",
    "error: unauthorised", "error: unauthorized",
)


def result_error_signature(result: str) -> str | None:
    """The first error signature found in a goal result, or None."""
    low = (result or "").lower()
    for sig in _RESULT_ERROR_SIGNATURES:
        if sig in low:
            return sig
    return None


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
    wake_provenance: str = "wake_nudge",
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

    ``wake_provenance`` labels the stored wake row. Default ``wake_nudge``
    reads as silence-expected (dispatch_runner skips the send-tool rescue for
    nudge-only turns); callers whose wake MUST speak user-facing output —
    background-task relays (backburner), which exist to deliver a result —
    pass ``task_relay`` so the rescue applies when the model skips its send
    call (2026-08-30: a detached AFL turn's finished relay was silently
    dropped this way).
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

    # Goal rooms: the room's routes die with the goal (plan D11 — orphan
    # routes waking a dead room are the "3 dead routines" lesson again), and
    # a cancelled room-goal parent cascades to its children with the reason
    # recorded (completion requires children settled first — enforced at the
    # room_close door and here for operator/dashboard settles).
    from server.services import goal_rooms
    if goal_rooms.is_room_session(goal["conversation_id"]):
        try:
            await goal_rooms.prune_room_routes(ctx, goal_id)
        except Exception:
            logger.exception("goal %s: room route prune failed", goal_id)
        if status == "cancelled":
            for child in await repo.children_of(goal_id, status="active"):
                await settle_goal(
                    ctx, child["id"], status="cancelled",
                    result=f"parent goal cancelled: {objective_of(goal)}",
                    note=f"cascade from parent {goal_id}")

    parent_id = goal.get("parent_goal_id")
    if parent_id:
        await _roll_up_to_parent(ctx, goal, status, result)
        return True

    origin = goal["origin_conversation_id"]
    if wake_origin and origin and origin != goal["conversation_id"]:
        from server.services.wake_service import wake_conversation

        sig = result_error_signature(result) if status == "completed" else None
        if sig:
            logger.warning(
                "goal %s completed but its result carries error signature "
                "%r — the wake is flagged so it can't be narrated as success",
                goal_id, sig)
        content = wake_content or (
            (f"## Goal {status} — ⚠ OUTPUT CONTAINS AN ERROR ({sig}): the "
             f"job likely did NOT succeed. Read the full result below "
             f"before reporting anything.\n"
             if sig else f"## Goal {status}\n")
            + f"Objective: {goal['objective']}\n\n"
            + f"{result}"
        )
        try:
            await wake_conversation(
                ctx, origin, content,
                call_category=wake_category or "goal_result",
                metadata={"goal_id": goal_id, "goal_kind": goal["kind"]},
                provenance=wake_provenance,
            )
        except Exception:
            logger.exception("goal %s: failed to wake origin %s", goal_id, origin)
    return True


def objective_of(goal: dict[str, Any]) -> str:
    return (goal.get("objective") or "")[:200]


async def _roll_up_to_parent(
    ctx: AppContext, child: dict[str, Any], status: str, result: str,
) -> None:
    """Child-settle roll-up (plan §1.2): enqueue a durable reviser run on the
    parent with the child's outcome as the stimulus. If effect enqueueing
    itself fails, degrade to a direct wake of the parent's working
    conversation — information must not be lost to infrastructure.

    Goal rooms (docs/goal-rooms-plan.md D10): a parent with a room has a
    thinker — the roll-up is a direct wake of the parent room (the child's
    report lands in its history, in context), no reviser, no patch."""
    stimulus = (
        f"## Child goal {status}\n"
        + (f"⚠ OUTPUT CONTAINS AN ERROR — the child likely did NOT succeed. "
           f"Read the result.\n"
           if status == "completed" and result_error_signature(result) else "")
        + f"Objective: {child['objective']}\n\n"
        + f"Result: {result}"
    )
    from server.services import goal_rooms
    parent = await GoalRepository(ctx.db).get(child["parent_goal_id"])
    if parent is not None and goal_rooms.is_room_session(parent["conversation_id"]):
        from server.services.wake_service import wake_conversation
        try:
            await wake_conversation(
                ctx, parent["conversation_id"], stimulus,
                call_category="goal_progress",
                metadata={"goal_id": parent["id"],
                          "rolled_up_from": child["id"]},
            )
        except Exception:
            logger.exception("goal %s: room roll-up wake failed", child["id"])
        return

    # Legacy child settle (non-room parent): the reviser fold is retired
    # (task-registry Phase 4) — the parent gets the stimulus directly.
    from server.services.wake_service import wake_conversation
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
        logger.exception("goal %s: roll-up wake failed", child["id"])


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
      routine     — look up the routine definition and dispatch it (detached,
                    so a slow LLM run never blocks the pump). Deleted/disabled
                    routines end their series; a validity-window miss skips
                    the run but keeps the series alive.
      goal_checkin — a goal room's check-in (goal-rooms plan D13): wakes the
                    ROOM with the state block, the claims digest since the
                    last check-in, and the pick-up-the-thread brief.
      wake        — goal-deadline or plain scheduled wake for a conversation.
    """
    from server.services.wake_service import wake_conversation

    if wakeup.get("kind") == "routine":
        from server.services import routine_service as routines

        payload = json.loads(wakeup.get("payload_json") or "{}")
        routine = await routines.RoutineService(ctx).get_by_id(
            payload.get("routine_id", ""))
        if not routine or not routine["enabled"]:
            return False  # definition gone/disabled: series ends
        # Keep the routines-row next_run_at mirror in step with the wakeup
        # series (both compute from now) — the routine tools echo it as
        # next_fire, and a stale mirror reads as a past fire date.
        await routines.RoutineService(ctx).advance_next_run(routine)
        if routines._outside_validity_window(routine):
            return True   # skip this run, keep the schedule
        await routines.append_fired_event(ctx, routine, wakeup["not_before"])
        routines.fire_routine_detached(ctx, routine)
        return True

    goal = None
    if wakeup["goal_id"]:
        goal = await GoalRepository(ctx.db).get(wakeup["goal_id"])
        if goal and goal["status"] != "active":
            return True  # goal already settled; wakeup is moot

    if wakeup.get("kind") == "task_due":
        # Task-registry backstop (docs/task-registry-plan.md): completion
        # didn't arrive by the due time — wake the WAITER so it decides
        # (chase, re-register, or cancel). The settle effect cancels this
        # series' future when the task resolves.
        from server.services import tasks as task_svc
        payload = json.loads(wakeup.get("payload_json") or "{}")
        task = None
        if payload.get("task_id"):
            from server.repositories.tasks import TaskRepository
            task = await TaskRepository(ctx.db).get(payload["task_id"])
        if task is not None and task["status"] != "pending":
            return False  # settled between arm and fire; series ends
        label = (f" ({task['title']})" if task else "")
        await wake_conversation(
            ctx, wakeup["conversation_id"],
            f"## Task overdue{label}\n"
            f"A task you were waiting on passed its due time without "
            f"settling. list_tasks to see it; chase the completer, "
            f"re-register, or task_cancel it.",
            call_category="task_due",
            metadata={"wakeup_id": wakeup["id"],
                      "task_id": payload.get("task_id")},
        )
        return False  # one-shot backstop per registration

    if wakeup.get("kind") == "goal_checkin":
        # Room check-in: the room renders its own brief (state + digest +
        # standing decisions). Series rolls via the wakeup recurrence.
        from server.services import goal_rooms
        if goal is None:
            return True  # room's goal gone; series moot
        content = await goal_rooms.render_checkin(ctx, goal)
        await wake_conversation(
            ctx, goal["conversation_id"], content,
            call_category="goal_checkin",
            metadata={"wakeup_id": wakeup["id"], "goal_id": goal["id"]},
        )
        return True

    if wakeup.get("kind") == "goal_continue":
        # Goal loop (docs/goal-execution-plan.md): the goal's single
        # continuation slot fired. One-shot — the NEXT slot is scheduled by
        # the settle path (end_turn), never by recurrence.
        from server.services import goal_loop
        if goal is None or goal["status"] != "active":
            return False  # settled since armed; no series to keep
        payload = json.loads(wakeup.get("payload_json") or "{}")
        content = await goal_loop.handle_continue_wakeup(ctx, goal, payload)
        await wake_conversation(
            ctx, goal["conversation_id"], content,
            call_category="goal_continue",
            metadata={"wakeup_id": wakeup["id"], "goal_id": goal["id"],
                      "loop_kind": goal_loop.CONTINUE_KIND,
                      "frame": payload.get("frame")},
        )
        return False

    if wakeup.get("kind") == "goal_deadman":
        # Liveness floor (D3): fires only when the room has been silent a
        # full interval. Recurring series — return True to keep it.
        from server.services import goal_loop
        if goal is None or goal["status"] != "active":
            return False
        if not goal_loop.loop_enabled(ctx):
            return False  # kill switch: the series ends, no zombie pulses
        content = await goal_loop.handle_deadman_wakeup(ctx, goal)
        if content is None:
            return True  # healthy room; skip this occurrence
        await wake_conversation(
            ctx, goal["conversation_id"], content,
            call_category="goal_deadman",
            metadata={"wakeup_id": wakeup["id"], "goal_id": goal["id"],
                      "loop_kind": goal_loop.DEADMAN_KIND},
        )
        return True

    if goal:
        content = (
            f"## Goal deadline reached\n"
            f"Objective: {goal['objective']}\n"
            f"Status: still active (no result yet)\n"
            f"Progress: {goal['progress'] or 'none recorded'}\n\n"
            "Decide how to proceed: act on the goal state's next_actions and "
            "any authorised follow-up ladder (e.g. re-DM, then email, then "
            "call), revise the goal, or — if the objective is already met by "
            "answers that arrived on any channel — close it with a result. "
            "Follow up with individuals directly; do not post status checks "
            "to groups."
        )
        category = "goal_deadline"
        # Deadline wakes land in the WORKING conversation (wake matrix
        # 2026-09-16) — re-resolved at fire time so rows scheduled under
        # the old origin-targeting rule also deliver to the worker.
        target = await _wakeup_target(GoalRepository(ctx.db), goal)
    else:
        content = "## Scheduled wakeup\nA scheduled wakeup for this conversation fired."
        category = "wakeup"
        target = wakeup["conversation_id"]

    await wake_conversation(
        ctx, target, content,
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
            from server.cron import next_cron_occurrence
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


