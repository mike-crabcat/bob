"""Dashboard API: Work — everything owed, in one view (commitments plan Phase 6).

Active goals as a tree (with the promises under each, the room's principal,
the next wake and live runs), standalone promises, pending suggestions, and
everything running in the background with a link to its turn trace. Goal
detail stays on /goals/{id}; this is the cross-cutting view.
"""

from __future__ import annotations

from fastapi import APIRouter

from server.routers.dashboard_api._common import *  # noqa: F403,F405


router = APIRouter()


@router.get("/api/work")
async def get_work(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)

    from server.repositories.contacts import ContactRepository
    from server.repositories.goals import GoalRepository
    from server.repositories.llm_call_log import LlmCallLogRepository
    from server.repositories.runs import RunRepository
    from server.repositories.tasks import TaskRepository
    from server.repositories.wakeups import WakeupRepository

    goals_repo = GoalRepository(db)
    active = await goals_repo.list_active(limit=200)
    ids = [g["id"] for g in active]
    next_wake = await WakeupRepository(db).next_for_goals(ids)
    promises = await goals_repo.promises_under(ids)
    runs = await RunRepository(db).running(limit=100)

    names: dict[str, str] = {}

    async def _who(contact_id: str | None) -> str | None:
        if not contact_id:
            return None
        if contact_id not in names:
            c = await ContactRepository(db).get(contact_id)
            names[contact_id] = (c or {}).get("name") or contact_id[:8]
        return names[contact_id]

    async def _trace(run: dict) -> dict | None:
        """The live call a running flight is on (its turn timeline)."""
        if not run.get("dispatch_id"):
            return None
        call = await LlmCallLogRepository(db).get_running_by_dispatch(run["dispatch_id"])
        return {"call_id": call["id"], "session_key": run["session_key"]} if call else None

    def _run_view(r: dict) -> dict:
        return {"id": r["id"], "kind": r["kind"], "summary": r["summary"][:200],
                "session_key": r["session_key"], "started_at": _utc(r["started_at"])}

    nodes = []
    for g in active:
        sessions = {g["conversation_id"], g.get("origin_conversation_id")}
        under = [p for p in promises if p["source_goal_id"] == g["id"]]
        nodes.append({
            "id": g["id"],
            "objective": g["objective"],
            "label": g["kind"],
            "profile": g.get("profile") or "outcome",
            "parent_goal_id": g.get("parent_goal_id"),
            "conversation_id": g["conversation_id"],
            "origin_conversation_id": g.get("origin_conversation_id"),
            "principal": await _who(g.get("creator_contact_id")),
            "deadline": _utc(g["deadline"]) if g.get("deadline") else None,
            "next_wake": ({"at": _utc(next_wake[g["id"]]["not_before"]),
                           "kind": next_wake[g["id"]]["kind"]}
                          if g["id"] in next_wake else None),
            "promises": [{"id": p["id"], "text": p["objective"],
                          "completer": p["completer"], "status": p["status"]}
                         for p in under],
            "runs": [_run_view(r) for r in runs if r["session_key"] in sessions],
        })

    standalone = [
        {"id": t["id"], "text": t["title"], "waiter": t["waiter_session"],
         "completer": t["expected_completer"], "due": _utc(t["due"]) if t["due"] else None}
        for t in await TaskRepository(db).pending_all(limit=100)
        if t["source_goal_id"] not in set(ids)
    ]
    suggestions = [
        {"id": s["id"], "text": s["objective"], "session_key": s["origin_conversation_id"],
         "created_at": _utc(s["created_at"])}
        for s in await goals_repo.pending_suggestions(limit=50)
    ]
    background = []
    for r in runs:
        view = _run_view(r)
        view["trace"] = await _trace(r)
        background.append(view)

    return {"goals": nodes, "promises": standalone,
            "suggestions": suggestions, "background": background}
