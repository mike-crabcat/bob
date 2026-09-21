"""Dashboard API: Goals & wakeups (Dashboard v3 increment 3).

Read views over goals + transitions and scheduled wakeups, plus operator
cancel actions (goal settle via goal_service so wakeups are cancelled and
the origin conversation is notified; wakeup cancel via WakeupRepository).
"""

from __future__ import annotations

from fastapi import APIRouter

from server.routers.dashboard_api._common import *  # noqa: F403,F405
from server.repositories.goals import GoalRepository
from server.repositories.wakeups import WakeupRepository


router = APIRouter()


@router.get("/api/goals")
async def list_goals(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)

    rows = await GoalRepository(db).list_recent(limit=100)
    goals = [
        {
            "id": r["id"],
            "conversation_id": r["conversation_id"],
            "origin_conversation_id": r["origin_conversation_id"],
            "parent_goal_id": r.get("parent_goal_id"),
            "kind": r["kind"],
            "objective": r["objective"],
            "progress": r["progress"],
            "result": (r["result"] or "")[:300],
            "status": r["status"],
            "deadline": _utc(r["deadline"]) if r["deadline"] else None,
            "created_at": _utc(r["created_at"]),
            "updated_at": _utc(r["updated_at"]),
        }
        for r in rows
    ]

    # Goal-loop summaries (docs/goal-execution-plan.md D10): next run from
    # the single continuation slot, budget burn, open branches, flags.
    import json as _json
    from server.repositories.tasks import TaskRepository
    from server.repositories.wakeups import WakeupRepository
    from server.services.goal_loop import state_of
    goal_ids = [g["id"] for g in goals]
    slots: dict[str, dict[str, Any]] = {}
    for w in await WakeupRepository(db).pending_kind_rows(
            goal_ids, "goal_continue"):
        payload = {}
        try:
            payload = _json.loads(w["payload_json"] or "{}")
        except (TypeError, ValueError):
            pass
        slots[w["goal_id"]] = {
            "at": _utc(w["not_before"]),
            "frame": payload.get("frame"),
        }
    open_tasks = await TaskRepository(db).open_counts_by_goal(goal_ids)
    for g, r in zip(goals, rows):
        loop = state_of(r)
        g["loop"] = {
            "on": bool(loop),
            "budget_total": loop.get("budget_total"),
            "budget_spent": loop.get("budget_spent"),
            "stall_streak": loop.get("stall_streak", 0),
            "skip_streak": loop.get("skip_streak", 0),
            "open_tasks": open_tasks.get(g["id"], 0),
            "next_run": slots.get(g["id"]),
        }

    goal_ids = [g["id"] for g in goals]
    children_by_goal = await GoalRepository(db).children_map(goal_ids)

    from server.services.goal_state_service import parse_strategy
    for g, r in zip(goals, rows):
        g["children"] = children_by_goal.get(g["id"], [])
        state = parse_strategy(r)
        g["state"] = {
            "plan": state.plan[:200],
            "known": len(state.known),
            "open_questions": state.open_questions[:5],
            "next_actions": [
                {"action": na.action[:160], "due": na.due}
                for na in state.next_actions[:5]],
            "entities": state.refs.entities[:8],
        }

    transitions_by_goal: dict[str, list[dict[str, Any]]] = {}
    if goal_ids:
        for t in await GoalRepository(db).transitions_for(goal_ids):
            transitions_by_goal.setdefault(t["goal_id"], []).append({
                "from_status": t["from_status"],
                "to_status": t["to_status"],
                "note": t["note"],
                "created_at": _utc(t["created_at"]),
            })
    for g in goals:
        g["transitions"] = transitions_by_goal.get(g["id"], [])
    return {"goals": goals}


@router.get("/api/wakeups")
async def list_wakeups(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    wakeups = [
        {
            "id": r["id"],
            "conversation_id": r["conversation_id"],
            "goal_id": r["goal_id"],
            "kind": r["kind"],
            "not_before": _utc(r["not_before"]),
            "recurrence": r["recurrence"],
            "tz": r["tz"],
            "status": r["status"],
            "payload": (r["payload_json"] or "")[:300],
            "created_at": _utc(r["created_at"]),
        }
        for r in await WakeupRepository(db).list_all_scheduled(limit=100)
    ]
    recent = [
        {
            "id": r["id"],
            "conversation_id": r["conversation_id"],
            "kind": r["kind"],
            "not_before": _utc(r["not_before"]),
            "recurrence": r["recurrence"],
            "status": r["status"],
        }
        for r in await WakeupRepository(db).recent_settled(limit=30)
    ]
    return {"scheduled": wakeups, "recent": recent}


@router.post("/api/goals/{goal_id}/cancel")
async def cancel_goal(goal_id: str, request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    from server.context import AppContext
    from server.services.goal_service import settle_goal

    ctx = AppContext(db=_db(request), settings=request.app.state.settings)
    ok = await settle_goal(
        ctx, goal_id, status="cancelled",
        result="Cancelled by operator from the dashboard.",
        note="dashboard cancel")
    if not ok:
        return {"ok": False, "error": "goal not found or already settled"}
    logger.info("dashboard: goal %s cancelled by operator", goal_id)
    return {"ok": True}


@router.post("/api/wakeups/{wakeup_id}/cancel")
async def cancel_wakeup(wakeup_id: str, request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    from server.repositories.wakeups import WakeupRepository

    ok = await WakeupRepository(_db(request)).cancel(wakeup_id)
    if not ok:
        return {"ok": False, "error": "wakeup not found or not scheduled"}
    logger.info("dashboard: wakeup %s cancelled by operator", wakeup_id)
    return {"ok": True}


@router.get("/api/goals/{goal_id}")
async def goal_detail(goal_id: str, request: Request) -> dict[str, Any]:
    """The follow-along surface (goal-execution-plan D10): state block with
    the strategies tree, loop budget + next run, every branch (task) tied
    to the goal, and the wake/turn timeline. Read-only."""
    import json as _json
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)

    goal = await GoalRepository(db).get(goal_id)
    if goal is None:
        return {"error": "goal not found"}

    from server.repositories.history import HistoryRepository
    from server.repositories.tasks import TaskRepository
    from server.repositories.wakeups import WakeupRepository
    from server.services.goal_loop import state_of
    from server.services.goal_state_service import parse_strategy
    state = parse_strategy(goal)

    loop = state_of(goal)
    next_run = None
    if goal["status"] == "active":
        slot = await WakeupRepository(db).pending_of_kind(
            goal_id, "goal_continue")
        if slot is not None:
            try:
                payload = _json.loads(slot["payload_json"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            next_run = {"at": _utc(slot["not_before"]),
                        "frame": payload.get("frame"),
                        "note": (payload.get("note") or "")[:200]}

    branches = await TaskRepository(db).list_for_goal(
        goal_id, goal["conversation_id"], limit=60)
    wakes = await WakeupRepository(db).list_for_goal(goal_id, limit=40)
    turns = [
        {"role": t["role"], "provenance": t.get("provenance"),
         "content": t.get("content") or "",
         "created_at": t.get("created_at")}
        for t in await HistoryRepository(db).recent_messages(
            goal["conversation_id"], limit=25)
    ]

    return {
        "goal": {
            "id": goal["id"], "kind": goal["kind"],
            "objective": goal["objective"], "status": goal["status"],
            "progress": goal["progress"], "result": goal["result"],
            "deadline": _utc(goal["deadline"]) if goal["deadline"] else None,
            "conversation_id": goal["conversation_id"],
            "origin_conversation_id": goal["origin_conversation_id"],
            "version": goal.get("version"),
            "created_at": _utc(goal["created_at"]),
            "updated_at": _utc(goal["updated_at"]),
        },
        "state": {
            "plan": state.plan,
            "known": state.known[-50:],
            "open_questions": state.open_questions[:10],
            "next_actions": [{"action": na.action, "due": na.due}
                             for na in state.next_actions[:10]],
            "strategies": [s.model_dump() if hasattr(s, "model_dump") else dict(s)
                           for s in state.strategies],
            "entities": state.refs.entities[:12],
        },
        "loop": {
            "on": bool(loop),
            "budget_total": loop.get("budget_total"),
            "budget_spent": loop.get("budget_spent"),
            "stall_streak": loop.get("stall_streak", 0),
            "skip_streak": loop.get("skip_streak", 0),
            "last_frame": loop.get("last_frame"),
            "last_delta": loop.get("last_delta"),
            "next_run": next_run,
        },
        "branches": [{
            "id": b["id"], "title": b["title"], "status": b["status"],
            "completer": b["expected_completer"], "due": _utc(b["due"]),
            "result": (b["result"] or b["error"] or "")[:300],
            "completed_at": _utc(b["completed_at"]),
            "created_at": _utc(b["created_at"]),
        } for b in branches],
        "wakes": [{
            "id": w["id"], "kind": w["kind"], "at": _utc(w["not_before"]),
            "status": w["status"], "created_at": _utc(w["created_at"]),
        } for w in wakes],
        "turns": [{
            "role": t["role"], "provenance": t["provenance"],
            "content": (t["content"] or "")[:400],
            "created_at": _utc(t["created_at"]),
        } for t in turns],
    }


@router.post("/api/goals/{goal_id}/pause")
async def pause_goal_loop(goal_id: str, request: Request) -> dict[str, Any]:
    """Cancel the goal's pending continuation slot (operator pause). The
    dead-man heartbeat stays — a paused goal still surfaces after a silent
    interval; pending branch events still wake the room."""
    if not _check_auth(request):
        return {"error": "unauthorized"}
    from server.context import AppContext
    from server.services.goal_loop import cancel_slot

    ctx = AppContext(db=_db(request), settings=request.app.state.settings)
    cancelled = await cancel_slot(ctx, goal_id)
    if not cancelled:
        return {"ok": False, "error": "no pending continuation slot"}
    logger.info("dashboard: goal %s loop paused by operator", goal_id)
    return {"ok": True}
