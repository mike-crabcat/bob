"""Dashboard API: Subagents."""

from __future__ import annotations

from fastapi import APIRouter

from bob_server.routers.dashboard_api._common import *  # noqa: F403,F405


router = APIRouter()


@router.get("/api/subagents")
async def get_subagents(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    rows = await db.fetch_all(
        """SELECT id, parent_session_key, session_key, task, status,
                  result, error_message, agent_type, cost_usd,
                  created_at, updated_at
           FROM subagents
           ORDER BY created_at DESC LIMIT 50"""
    )
    subagents: list[dict[str, Any]] = []
    for row in rows:
        subagents.append({
            "id": row["id"],
            "parent_session_key": row["parent_session_key"],
            "session_key": row["session_key"],
            "task_preview": (row["task"] or "")[:200],
            "status": row["status"],
            "result_preview": (row["result"] or "")[:200],
            "error_message": row["error_message"],
            "agent_type": row["agent_type"],
            "cost_usd": row["cost_usd"] or 0,
            "created_at": _utc(row["created_at"]),
            "updated_at": _utc(row["updated_at"]),
        })
    return {"subagents": subagents}


@router.get("/api/subagents/{subagent_id}")
async def get_subagent_detail(request: Request, subagent_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    row = await db.fetch_one(
        """SELECT id, parent_session_key, session_key, task, status,
                  result, error_message, agent_type, claude_session_id, cost_usd,
                  created_at, updated_at
           FROM subagents WHERE id = ?""",
        (subagent_id,),
    )
    if not row:
        return {"error": "not found"}
    return {
        "id": row["id"],
        "parent_session_key": row["parent_session_key"],
        "session_key": row["session_key"],
        "task": row["task"],
        "status": row["status"],
        "result": row["result"],
        "error_message": row["error_message"],
        "agent_type": row["agent_type"],
        "claude_session_id": row["claude_session_id"],
        "cost_usd": row["cost_usd"] or 0,
        "created_at": _utc(row["created_at"]),
        "updated_at": _utc(row["updated_at"]),
    }


@router.post("/api/subagents/{subagent_id}/message")
async def message_subagent(request: Request, subagent_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        return {"ok": False, "error": "message is required"}

    from bob_server.context import AppContext
    from bob_server.services.subagent_service import SubagentService

    ctx = AppContext(
        db=_db(request),
        settings=request.app.state.settings,
        event_bus=getattr(request.app.state, "event_bus", None),
    )
    svc = SubagentService(ctx)
    try:
        result = await svc.message_subagent(subagent_id, message)
        return result
    except Exception as exc:
        logger.error("Subagent message failed: %s", exc)
        return {"ok": False, "error": str(exc)}


@router.post("/api/subagents/{subagent_id}/kill")
async def kill_subagent(request: Request, subagent_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}

    from bob_server.context import AppContext
    from bob_server.services.subagent_service import SubagentService

    ctx = AppContext(
        db=_db(request),
        settings=request.app.state.settings,
        event_bus=getattr(request.app.state, "event_bus", None),
    )
    svc = SubagentService(ctx)
    try:
        result = await svc.kill_subagent(subagent_id)
        return result
    except Exception as exc:
        logger.error("Subagent kill failed: %s", exc)
        return {"ok": False, "error": str(exc)}


# ── Phone ────────────────────────────────────────────────────────────────────


