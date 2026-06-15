"""Dashboard API: Frontend error logging."""

from __future__ import annotations

from fastapi import APIRouter

from bob_server.routers.dashboard_api._common import *  # noqa: F403,F405


router = APIRouter()


@router.post("/api/frontend-errors")
async def log_frontend_error(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    try:
        body = await request.json()
    except Exception:
        return {"ok": False}

    message = body.get("message", "Unknown frontend error")
    source = body.get("source", "")
    lineno = body.get("lineno", "")
    colno = body.get("colno", "")
    stack = body.get("stack", "")
    url = body.get("url", "")

    logger.warning(
        "Frontend error: %s (at %s:%s:%s, url: %s)%s",
        message, source, lineno, colno, url,
        f"\n  stack: {stack}" if stack else "",
    )
    return {"ok": True}


# ── Skills ──────────────────────────────────────────────────────────────────


