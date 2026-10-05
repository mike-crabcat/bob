"""Dashboard API: Subagents."""

from __future__ import annotations

from fastapi import APIRouter

from server.routers.dashboard_api._common import *  # noqa: F403,F405


router = APIRouter()


@router.get("/api/subagents")
async def get_subagents(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    from server.repositories.subagents import SubagentRepository
    rows = await SubagentRepository(db).recent(limit=50)
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
    # Background flights live in runs since 2026-10-05 (commitments plan
    # Phase 0) — shown here in the same shape so the page keeps them.
    from server.repositories.runs import RunRepository
    for run in await RunRepository(db).recent(limit=50):
        if run["kind"] != "flight":
            continue
        subagents.append(_run_as_subagent(run, preview=200))
    subagents.sort(key=lambda r: r["created_at"] or "", reverse=True)
    return {"subagents": subagents[:50]}


def _run_as_subagent(run: dict[str, Any], *, preview: int | None = None) -> dict[str, Any]:
    """A flight run in the subagents API shape. ``preview`` → list shape
    (task_preview/result_preview, truncated); None → detail shape."""
    task, result = run["summary"] or "", run["result"] or ""
    shaped: dict[str, Any] = {
        "id": run["id"],
        "parent_session_key": run["session_key"],
        "session_key": run["session_key"],
        "status": run["status"],
        "error_message": run["error_message"],
        "agent_type": "detached_turn",
        "cost_usd": 0,
        "created_at": _utc(run["started_at"]),
        "updated_at": _utc(run["ended_at"] or run["started_at"]),
    }
    if preview:
        shaped["task_preview"], shaped["result_preview"] = task[:preview], result[:preview]
    else:
        shaped["task"], shaped["result"] = task, result
    return shaped


@router.get("/api/subagents/{subagent_id}")
async def get_subagent_detail(request: Request, subagent_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    from server.repositories.subagents import SubagentRepository
    row = await SubagentRepository(db).get(subagent_id)
    if not row:
        from server.repositories.runs import RunRepository
        run = await RunRepository(db).get(subagent_id)
        if not run:
            return {"error": "not found"}
        return {**_run_as_subagent(run), "claude_session_id": None}
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

    from server.context import AppContext
    from server.services.subagent_service import SubagentService

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

    from server.context import AppContext
    from server.services.subagent_service import SubagentService

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


