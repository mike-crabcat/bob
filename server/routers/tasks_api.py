"""Task-settle endpoint — the script-facing completion path
(docs/task-registry-plan.md D10, token per Mike's Q1 ruling).

POST /api/v1/tasks/{task_id}/settle   Bearer BOB_TASK_TOKEN
    {"status": "completed"|"failed", "result"|"error": "...", "by": "name"}

gives bg_start processes (watchers, pollers, ledger monitors) a callback
with no LLM — the square that was missing from the bg-grid. Settle is
CAS-once; retries and double-POSTs return the settled state, never double.
"""

from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from server.context import AppContext
from server.routers.stimulus import _bearer_token
from server.dependencies import get_app_context

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


def _task_token() -> str:
    return os.environ.get("BOB_TASK_TOKEN", "").strip()


@router.post("/{task_id}/settle")
async def settle_task_endpoint(
    task_id: str,
    request: Request,
    ctx: AppContext = Depends(get_app_context),
) -> Any:
    token = _task_token()
    if not token:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "task settle not configured (no BOB_TASK_TOKEN)")
    if not hmac.compare_digest(_bearer_token(request), token):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Unauthorized: valid task token required",
            headers={"WWW-Authenticate": 'Bearer realm="bob-tasks"'})

    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON")

    to_status = str(data.get("status") or "completed").strip().lower()
    if to_status not in ("completed", "failed"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "status must be completed or failed")
    completed_by = f"script:{str(data.get('by') or 'external')[:80]}"
    result = str(data.get("result") or "")[:8000] or None
    error = str(data.get("error") or "")[:8000] or None

    from server.services.tasks import settle_task
    out = await settle_task(
        ctx, task_id.strip(), to_status=to_status,
        result=result if to_status == "completed" else None,
        error=error if to_status == "failed" else None,
        completed_by=completed_by)
    if not out.get("ok") and out.get("error") == "task not found":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
    return out
