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
