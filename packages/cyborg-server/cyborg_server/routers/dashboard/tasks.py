"""Dashboard task routes."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse

from cyborg_server.context import AppContext
from cyborg_server.dependencies import get_app_context
from cyborg_server.models import TaskStatus

from ._helpers import _get_pending_approval_count, _get_settings, _render_template

router = APIRouter()


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_detail(
    task_id: str,
    request: Request,
    ctx: AppContext = Depends(get_app_context),
) -> Response:
    settings = _get_settings()
    db = ctx.db
    pending_count = await _get_pending_approval_count(db)

    task = await db.fetch_one(
        "SELECT * FROM tasks WHERE id = ? AND deleted_at IS NULL",
        (task_id,),
    )
    if not task:
        return _render_template(
            "dashboard/base.html",
            request,
            {"version": settings.version, "pending_count": pending_count},
        )

    project_row = await db.fetch_one(
        "SELECT project_id FROM project_tasks WHERE task_id = ?",
        (task_id,),
    )
    project_id = project_row["project_id"] if project_row else None
    project_info = None
    if project_id:
        p = await db.fetch_one("SELECT id, title FROM projects WHERE id = ?", (project_id,))
        if p:
            project_info = {"id": p["id"], "title": p.get("title")}

    steps = await db.fetch_all(
        "SELECT * FROM task_steps WHERE task_id = ? ORDER BY step_number ASC",
        (task_id,),
    )

    task_files = await db.fetch_all(
        "SELECT * FROM task_files WHERE task_id = ? ORDER BY created_at DESC",
        (task_id,),
    )

    timeline: list[dict[str, Any]] = []

    history_rows = await db.fetch_all(
        "SELECT action, details, timestamp FROM task_history WHERE task_id = ?",
        (task_id,),
    )
    for row in history_rows:
        timeline.append({
            "type": "history",
            "timestamp": row["timestamp"],
            "label": row["action"].replace("_", " ").title() if row["action"] else "Event",
            "summary": row["details"][:200] if row["details"] else "",
        })

    notif_rows = await db.fetch_all(
        "SELECT notification_type, title, message, status, delivery_status, created_at FROM notifications WHERE entity_type = 'task' AND entity_id = ?",
        (task_id,),
    )
    for row in notif_rows:
        msg = row["message"] or ""
        summary = (row["title"] + ": " + msg) if row["title"] else msg
        timeline.append({
            "type": "notification",
            "timestamp": row["created_at"],
            "label": row["notification_type"].replace("_", " ").title(),
            "summary": summary[:200],
            "status": row.get("status"),
        })

    prompt_rows = await db.fetch_all(
        "SELECT category, prompt_text, token_count_estimate, timestamp, session_key FROM prompt_history WHERE task_id = ?",
        (task_id,),
    )
    for row in prompt_rows:
        timeline.append({
            "type": "prompt",
            "timestamp": row["timestamp"],
            "label": row["category"].replace("_", " ").title(),
            "summary": row["prompt_text"][:200] if row["prompt_text"] else "",
            "full_text": row["prompt_text"],
            "tokens": row["token_count_estimate"],
            "session_key": row["session_key"],
        })

    approval_rows = await db.fetch_all(
        "SELECT id, title, status, input_schema, input_response, requested_at, reviewed_at FROM approvals WHERE approval_type = 'task_input' AND entity_id = ?",
        (task_id,),
    )
    for row in approval_rows:
        timeline.append({
            "type": "approval",
            "timestamp": row["requested_at"],
            "label": "Input Request",
            "summary": row["title"] or "Task input requested",
            "status": row["status"],
            "approval_id": row["id"],
        })
        if row["reviewed_at"]:
            timeline.append({
                "type": "approval",
                "timestamp": row["reviewed_at"],
                "label": ("Input Approved" if row["status"] == "approved" else "Input Rejected"),
                "summary": row["title"] or "Task input resolved",
                "status": row["status"],
                "approval_id": row["id"],
            })

    timeline.sort(key=lambda item: item.get("timestamp") or "", reverse=True)
    timeline = timeline[:100]

    pending_approval = None
    if task["status"] == "blocked":
        pa = await db.fetch_one(
            "SELECT id, title, description, input_schema, proposal_data FROM approvals WHERE approval_type = 'task_input' AND entity_id = ? AND status = 'pending' LIMIT 1",
            (task_id,),
        )
        if pa:
            pending_approval = dict(pa)
            pending_approval["proposal"] = None
            if pa.get("proposal_data"):
                try:
                    pending_approval["proposal"] = json.loads(pa["proposal_data"])
                except (json.JSONDecodeError, TypeError):
                    pass
            pending_approval["input_schema_parsed"] = None
            if pa.get("input_schema"):
                try:
                    pending_approval["input_schema_parsed"] = json.loads(pa["input_schema"])
                    schema = pending_approval["input_schema_parsed"]
                    if project_id and schema.get("type") == "multi_choice":
                        for option in schema.get("options", []):
                            if option.get("image_url"):
                                option["image_url"] = f"/dashboard/projects/{project_id}/files/{option['image_url']}"
                            if option.get("audio_url"):
                                option["audio_url"] = f"/dashboard/projects/{project_id}/files/{option['audio_url']}"
                except (json.JSONDecodeError, TypeError):
                    pass

    return _render_template(
        "dashboard/task_detail.html",
        request,
        {
            "version": settings.version,
            "pending_count": pending_count,
            "task": dict(task),
            "project": project_info,
            "steps": steps,
            "task_files": task_files,
            "timeline": timeline,
            "pending_approval": pending_approval,
        },
    )


@router.post("/tasks/{task_id}/tap")
async def tap_task(
    task_id: str,
    ctx: AppContext = Depends(get_app_context),
) -> Response:
    from fastapi.responses import JSONResponse

    db = ctx.db
    task = await db.fetch_one(
        "SELECT id, status FROM tasks WHERE id = ? AND deleted_at IS NULL",
        (task_id,),
    )
    if not task:
        return JSONResponse(content={"error": "Task not found"}, status_code=404)

    if task["status"] != TaskStatus.ACTIVE.value:
        return JSONResponse(content={"error": "Task is not active"}, status_code=400)

    from cyborg_server.services.notification_service import NotificationService
    notification_service = NotificationService(ctx)
    notification_id = await notification_service.create_task_tap_notification(task_id)

    if notification_id is None:
        return JSONResponse(content={"error": "Task has no delivery route"}, status_code=422)

    return JSONResponse(content={"status": "ok", "task_id": task_id})
