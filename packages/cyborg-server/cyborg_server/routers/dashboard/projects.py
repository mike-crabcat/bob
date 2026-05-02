"""Dashboard project routes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from fastapi.responses import HTMLResponse

from cyborg_server.context import AppContext
from cyborg_server.dependencies import get_app_context

from ._helpers import (
    _get_pending_approval_count,
    _get_settings,
    _group_files_by_category,
    _render_template,
    _scan_project_files,
)

router = APIRouter()


@router.get("/projects", response_class=HTMLResponse)
async def projects(
    request: Request,
    ctx: AppContext = Depends(get_app_context),
    status: str | None = None,
) -> Response:
    settings = _get_settings()
    db = ctx.db

    query = "SELECT * FROM projects WHERE deleted_at IS NULL"
    params = []

    if status:
        query += " AND state = ?"
        params.append(status)

    query += " ORDER BY created_at DESC LIMIT 50"

    projects_data = await db.fetch_all(query, tuple(params))

    projects = []
    for p in projects_data:
        project_id = p["id"]

        task_counts = await db.fetch_one(
            """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed
            FROM tasks t
            INNER JOIN project_tasks pt ON pt.task_id = t.id
            WHERE pt.project_id = ? AND t.deleted_at IS NULL
            """,
            (project_id,),
        )

        health = await db.fetch_one(
            """
            SELECT health_score, risk_level, created_at
            FROM latest_project_health
            WHERE project_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (project_id,),
        )

        projects.append({
            "id": project_id,
            "title": p.get("title"),
            "state": p["state"],
            "health_score": health["health_score"] if health else None,
            "completed_tasks": task_counts["completed"] if task_counts else 0,
            "total_tasks": task_counts["total"] if task_counts else 0,
            "created_at": p.get("created_at"),
        })

    pending_count = await _get_pending_approval_count(db)

    all_stats = await db.fetch_one(
        """
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN state = 'planning' THEN 1 ELSE 0 END) as planning,
            SUM(CASE WHEN state = 'active' THEN 1 ELSE 0 END) as active,
            SUM(CASE WHEN state = 'paused' THEN 1 ELSE 0 END) as paused,
            SUM(CASE WHEN state = 'closed' THEN 1 ELSE 0 END) as closed
        FROM projects WHERE deleted_at IS NULL
        """,
    )

    return _render_template(
        "dashboard/projects.html",
        request,
        {
            "version": settings.version,
            "projects": projects,
            "filter_status": status,
            "pending_count": pending_count,
            "stats": {
                "total": all_stats["total"] if all_stats else 0,
                "planning": all_stats["planning"] if all_stats else 0,
                "active": all_stats["active"] if all_stats else 0,
                "paused": all_stats["paused"] if all_stats else 0,
                "closed": all_stats["closed"] if all_stats else 0,
            },
        },
    )


@router.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_detail(
    project_id: str,
    request: Request,
    ctx: AppContext = Depends(get_app_context),
) -> Response:
    settings = _get_settings()
    db = ctx.db
    pending_count = await _get_pending_approval_count(db)

    project = await db.fetch_one(
        "SELECT * FROM projects WHERE id = ? AND deleted_at IS NULL",
        (project_id,),
    )

    if not project:
        return _render_template(
            "dashboard/base.html",
            request,
            {"version": settings.version, "pending_count": pending_count},
        )

    tasks = await db.fetch_all(
        """
        SELECT t.* FROM tasks t
        INNER JOIN project_tasks pt ON pt.task_id = t.id
        WHERE pt.project_id = ? AND t.deleted_at IS NULL
        ORDER BY t.created_at DESC
        LIMIT 50
        """,
        (project_id,),
    )

    journal = await db.fetch_all(
        """
        SELECT * FROM project_journal_entries
        WHERE project_id = ?
        ORDER BY created_at DESC
        LIMIT 20
        """,
        (project_id,),
    )

    health_checks = await db.fetch_all(
        """
        SELECT * FROM latest_project_health
        WHERE project_id = ?
        ORDER BY created_at DESC
        LIMIT 10
        """,
        (project_id,),
    )

    task_counts = await db.fetch_one(
        """
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END) as completed
        FROM tasks t
        INNER JOIN project_tasks pt ON pt.task_id = t.id
        WHERE pt.project_id = ? AND t.deleted_at IS NULL
        """,
        (project_id,),
    )

    project_data = dict(project)
    project_data["completed_tasks"] = task_counts["completed"] if task_counts else 0
    project_data["total_tasks"] = task_counts["total"] if task_counts else 0

    if project_data.get("success_criteria"):
        try:
            project_data["success_criteria"] = json.loads(project_data["success_criteria"])
        except (json.JSONDecodeError, TypeError):
            project_data["success_criteria"] = []

    prompts = await db.fetch_all(
        """
        SELECT ph.* FROM prompt_history ph
        WHERE ph.project_id = ?
           OR ph.task_id IN (SELECT pt.task_id FROM project_tasks pt WHERE pt.project_id = ?)
        ORDER BY ph.timestamp DESC
        LIMIT 50
        """,
        (project_id, project_id),
    )

    from cyborg_server.services.project_service import ProjectService
    project_service = ProjectService(ctx)
    project_path = await project_service.get_project_path(project_id)

    task_id_map: dict[str, str] = {}
    for t in tasks:
        short = t["id"].replace("-", "")[:8]
        task_id_map[short] = t["title"]

    scanned_files = _scan_project_files(project_path, task_id_map=task_id_map, task_file_limit=3, category_file_limit=10)
    file_categories = _group_files_by_category(scanned_files)

    specs = await db.fetch_all(
        """
        SELECT id, version_number, aim, method, plan, success_criteria,
               status, feedback, created_at, approved_at
        FROM project_specs
        WHERE project_id = ?
        ORDER BY version_number DESC
        """,
        (project_id,),
    )
    spec_list = []
    for s in specs:
        sd = dict(s)
        for field in ("plan", "success_criteria"):
            if sd.get(field):
                try:
                    sd[field] = json.loads(sd[field])
                except (json.JSONDecodeError, TypeError):
                    sd[field] = []
            else:
                sd[field] = []
        spec_list.append(sd)

    pending_approval = await db.fetch_one(
        """
        SELECT id, status FROM approvals
        WHERE entity_id = ? AND approval_type = 'project_plan' AND status = 'pending'
        ORDER BY created_at DESC LIMIT 1
        """,
        (project_id,),
    )

    return _render_template(
        "dashboard/project_detail.html",
        request,
        {
            "version": settings.version,
            "pending_count": pending_count,
            "project": project_data,
            "tasks": tasks,
            "journal": journal,
            "health_checks": health_checks,
            "prompts": prompts,
            "file_categories": file_categories,
            "file_count": len(scanned_files),
            "specs": spec_list,
            "pending_approval": dict(pending_approval) if pending_approval else None,
        },
    )


@router.post("/projects/{project_id}/delete")
async def delete_project(
    project_id: str,
    ctx: AppContext = Depends(get_app_context),
) -> Response:
    from cyborg_server.services.project_service import ProjectService
    project_service = ProjectService(ctx)
    await project_service.delete_project(project_id)
    return Response(
        status_code=303,
        headers={"Location": "/dashboard/projects"},
    )


@router.get("/projects/{project_id}/task-files/{task_short_id}")
async def dashboard_task_files_api(
    project_id: str,
    task_short_id: str,
    ctx: AppContext = Depends(get_app_context),
) -> Response:
    from fastapi.responses import JSONResponse
    from cyborg_server.services.project_service import ProjectService

    project_service = ProjectService(ctx)
    db = ctx.db
    project_path = await project_service.get_project_path(project_id)
    if not project_path:
        return JSONResponse(content={"files": []}, status_code=404)

    tasks = await db.fetch_all(
        "SELECT id, title FROM tasks t INNER JOIN project_tasks pt ON pt.task_id = t.id WHERE pt.project_id = ? AND t.deleted_at IS NULL",
        (project_id,),
    )
    task_id_map = {t["id"].replace("-", "")[:8]: t["title"] for t in tasks}

    scanned = _scan_project_files(project_path, task_id_map=task_id_map)
    task_files = [
        f for f in scanned
        if f.get("task_short_id") == task_short_id
    ]
    return JSONResponse(content={"files": task_files})


@router.get("/projects/{project_id}/category-files/{category}")
async def dashboard_category_files_api(
    project_id: str,
    category: str,
    ctx: AppContext = Depends(get_app_context),
) -> Response:
    from fastapi.responses import JSONResponse
    from cyborg_server.services.project_service import ProjectService

    project_service = ProjectService(ctx)
    project_path = await project_service.get_project_path(project_id)
    if not project_path:
        return JSONResponse(content={"files": []}, status_code=404)

    scanned = _scan_project_files(project_path)
    cat_files = [
        f for f in scanned
        if f.get("category") == category and "task_short_id" not in f
    ]
    return JSONResponse(content={"files": cat_files})


@router.post("/projects/{project_id}/pause")
async def dashboard_pause_project(
    project_id: str,
    ctx: AppContext = Depends(get_app_context),
) -> Response:
    from cyborg_server.services.project_service import ProjectService
    project_service = ProjectService(ctx)
    await project_service.pause_project(project_id)
    return Response(
        status_code=303,
        headers={"Location": f"/dashboard/projects/{project_id}"},
    )


@router.post("/projects/{project_id}/resume")
async def dashboard_resume_project(
    project_id: str,
    background_tasks: BackgroundTasks,
    ctx: AppContext = Depends(get_app_context),
) -> Response:
    from cyborg_server.services.project_service import ProjectService
    project_service = ProjectService(ctx)
    await project_service.resume_project(project_id)
    background_tasks.add_task(project_service.resume_project_reasoning, project_id)
    return Response(
        status_code=303,
        headers={"Location": f"/dashboard/projects/{project_id}"},
    )


@router.post("/projects/{project_id}/revise-spec")
async def dashboard_revise_spec(
    project_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    ctx: AppContext = Depends(get_app_context),
) -> Response:
    from cyborg_server.services.project_spec_service import ProjectSpecService

    form = await request.form()
    feedback = str(form.get("feedback", "")).strip()
    allow_aim = form.get("allow_aim_changes") is not None
    allow_criteria = form.get("allow_criteria_changes") is not None

    if not feedback:
        return Response(content="Feedback is required", status_code=400)

    spec_service = ProjectSpecService(ctx)
    db = ctx.db

    project = await db.fetch_one(
        "SELECT state FROM projects WHERE id = ? AND deleted_at IS NULL",
        (project_id,),
    )
    if project and project["state"] in ("closed", "paused"):
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "UPDATE projects SET state = 'planning', closed_at = NULL, conclusion = NULL, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
            (now, project_id),
        )
        await db.execute(
            "INSERT INTO project_journal_entries (id, project_id, entry_type, content, created_at, metadata) VALUES (?, ?, 'note', ?, ?, ?)",
            (str(uuid4()), project_id, f"Spec revision requested: {feedback[:200]}", now, "{}"),
        )

    async def _run_revision():
        try:
            await spec_service.revise_spec_after_rejection(
                project_id,
                feedback,
                allow_aim_changes=allow_aim,
                allow_criteria_changes=allow_criteria,
            )
        except Exception:
            pass

    background_tasks.add_task(_run_revision)

    return Response(
        status_code=303,
        headers={"Location": f"/dashboard/projects/{project_id}"},
    )


@router.get("/projects/{project_id}/files/{file_path:path}")
async def serve_project_file(
    project_id: str,
    file_path: str,
    ctx: AppContext = Depends(get_app_context),
) -> Response:
    from cyborg_server.services.project_service import ProjectService
    from starlette.responses import FileResponse as StarletteFileResponse
    import mimetypes

    project_service = ProjectService(ctx)
    project_path = await project_service.get_project_path(project_id)

    resolved = (project_path / file_path).resolve()
    workspace_root = project_path.resolve()

    if not str(resolved).startswith(str(workspace_root)):
        return Response(status_code=403)
    if not resolved.is_file():
        return Response(status_code=404)

    content_type, _ = mimetypes.guess_type(str(resolved))
    return StarletteFileResponse(
        str(resolved),
        media_type=content_type or "application/octet-stream",
    )
