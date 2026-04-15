"""Dashboard router for the Cyborg web interface.

Provides a self-hosted cyberpunk-themed dashboard for workflow monitoring,
project management, approvals review, and log viewing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from jinja2 import FileSystemLoader

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from fastapi.responses import HTMLResponse
from starlette.responses import StreamingResponse

from cyborg.config import Settings
from cyborg.database import Database
from cyborg.dependencies import get_database
from cyborg.models import ProjectState, TaskStatus


router = APIRouter(prefix="/dashboard", tags=["dashboard"])
logger = logging.getLogger(__name__)


def _get_settings() -> Settings:
    """Get application settings."""
    return Settings.from_env()


async def _get_pending_approval_count(db: Database) -> int:
    """Return the current number of pending dashboard approvals."""
    row = await db.fetch_one(
        "SELECT COUNT(*) as count FROM approvals WHERE status = 'pending'",
    )
    return int(row["count"]) if row and row["count"] else 0


async def _get_project_id_for_task(db: Database, task_id: str) -> str | None:
    """Look up the project_id for a task via the project_tasks join table."""
    row = await db.fetch_one("SELECT project_id FROM project_tasks WHERE task_id = ?", (task_id,))
    return row["project_id"] if row else None


def _approval_review_href(approval_type: str | None, entity_id: str | None, metadata: dict | None = None) -> str | None:
    """Return the best dashboard review link for an approval item."""
    if not entity_id:
        return None
    if approval_type in {"project_plan", "strategy_refinement", "follow_up_tasks"}:
        return f"/dashboard/projects/{entity_id}"
    if approval_type == "task_input":
        # For project-level blocks, entity_id IS the project_id
        if metadata and metadata.get("entity_kind") == "project":
            return f"/dashboard/projects/{entity_id}"
        return None  # Task input is handled inline in the approvals queue
    return None


@router.get("/", response_class=HTMLResponse)
async def overview(
    request: Request,
    db: Database = Depends(get_database),
) -> Response:
    """Workflow overview with real project, approval, and notification data."""
    settings = _get_settings()

    project_stats = await db.fetch_one(
        """
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN state = ? THEN 1 ELSE 0 END) as planning,
            SUM(CASE WHEN state = ? THEN 1 ELSE 0 END) as active,
            SUM(CASE WHEN state = ? THEN 1 ELSE 0 END) as paused,
            SUM(CASE WHEN state = ? THEN 1 ELSE 0 END) as closed
        FROM projects
        WHERE deleted_at IS NULL
        """,
        (
            ProjectState.PLANNING.value,
            ProjectState.ACTIVE.value,
            ProjectState.PAUSED.value,
            ProjectState.CLOSED.value,
        ),
    )
    planning_count = int(project_stats["planning"]) if project_stats and project_stats["planning"] else 0
    active_count = int(project_stats["active"]) if project_stats and project_stats["active"] else 0
    paused_count = int(project_stats["paused"]) if project_stats and project_stats["paused"] else 0
    closed_count = int(project_stats["closed"]) if project_stats and project_stats["closed"] else 0

    pending_count = await _get_pending_approval_count(db)

    urgent_approvals = await db.fetch_all(
        "SELECT COUNT(*) as count FROM approvals WHERE status = 'pending' AND priority = 'urgent'",
    )
    urgent_count = urgent_approvals[0]["count"] if urgent_approvals else 0

    open_notifications_row = await db.fetch_one(
        "SELECT COUNT(*) as count FROM notifications WHERE status = 'pending'",
    )
    open_notifications_count = (
        int(open_notifications_row["count"])
        if open_notifications_row and open_notifications_row["count"]
        else 0
    )

    health_snapshot_row = await db.fetch_one(
        "SELECT COUNT(*) as count FROM latest_project_health",
    )
    health_snapshot_count = (
        int(health_snapshot_row["count"])
        if health_snapshot_row and health_snapshot_row["count"]
        else 0
    )

    attention_rows = await db.fetch_all(
        """
        SELECT
            project_id,
            title,
            state,
            health_score,
            risk_level,
            last_check_at
        FROM projects_need_attention
        ORDER BY
            CASE risk_level
                WHEN 'critical' THEN 1
                WHEN 'high' THEN 2
                WHEN 'medium' THEN 3
                ELSE 4
            END,
            last_check_at DESC
        LIMIT 5
        """,
    )
    attention_projects = [
        {
            "id": row["project_id"],
            "title": row["title"],
            "state": row["state"],
            "health_score": row["health_score"],
            "risk_level": row["risk_level"],
            "last_checked": row["last_check_at"],
        }
        for row in attention_rows
    ]

    project_status_data = [
        planning_count,
        active_count,
        paused_count,
        closed_count,
    ]

    recent_journal = await db.fetch_all(
        """
        SELECT project_id, entry_type, content, created_at
        FROM project_journal_entries
        ORDER BY created_at DESC
        LIMIT 8
        """,
    )
    recent_notification_rows = await db.fetch_all(
        """
        SELECT
            n.*,
            CASE
                WHEN n.entity_type = 'project' THEN n.entity_id
                WHEN n.entity_type = 'task' THEN (
                    SELECT pt.project_id
                    FROM project_tasks pt
                    WHERE pt.task_id = n.entity_id
                    LIMIT 1
                )
                ELSE NULL
            END as project_id
        FROM notifications n
        ORDER BY n.created_at DESC
        LIMIT 8
        """,
    )
    recent_activities: list[dict[str, Any]] = []
    for entry in recent_journal:
        content = entry["content"] or ""
        recent_activities.append(
            {
                "created_at": entry["created_at"],
                "label": entry["entry_type"].replace("_", " ").title(),
                "kind": "journal",
                "title": entry["entry_type"].replace("_", " ").title(),
                "summary": content[:140] + "..." if len(content) > 140 else content,
                "link_url": f"/dashboard/projects/{entry['project_id']}" if entry.get("project_id") else None,
            }
        )
    for row in recent_notification_rows:
        message = row["message"] or ""
        recent_activities.append(
            {
                "created_at": row["created_at"],
                "label": row["notification_type"].replace("_", " ").title(),
                "kind": "notification",
                "title": row["title"],
                "summary": message[:140] + "..." if len(message) > 140 else message,
                "link_url": f"/dashboard/projects/{row['project_id']}" if row.get("project_id") else None,
            }
        )
    recent_activities.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    recent_activities = recent_activities[:10]

    pending_items = await db.fetch_all(
        """
        SELECT * FROM pending_approvals
        ORDER BY
            CASE priority
                WHEN 'urgent' THEN 1
                WHEN 'high' THEN 2
                WHEN 'normal' THEN 3
                WHEN 'low' THEN 4
            END,
            requested_at ASC
        LIMIT 10
        """,
    )
    pending_approval_items = []
    for item in pending_items:
        approval_type = item.get("approval_type", "unknown")
        proposal = None
        if item.get("proposal_data"):
            try:
                proposal = json.loads(item["proposal_data"])
            except (json.JSONDecodeError, TypeError):
                pass
        pending_approval_items.append({
            "id": item["id"],
            "type": approval_type,
            "type_label": approval_type.replace("_", " ").title(),
            "title": item["title"],
            "description": item.get("description", ""),
            "priority": item.get("priority", "normal"),
            "requested_at": _format_time(item["requested_at"]),
            "review_href": _approval_review_href(approval_type, item.get("entity_id")),
            "proposal": proposal,
        })

    task_stats = await db.fetch_all(
        """
        SELECT status, COUNT(*) as count
        FROM tasks
        WHERE deleted_at IS NULL
        GROUP BY status
        """,
    )
    task_status_counts = {row["status"]: row["count"] for row in task_stats}
    task_status_data = [
        task_status_counts.get("pending", 0),
        task_status_counts.get("active", 0),
        task_status_counts.get("blocked", 0),
        task_status_counts.get("completed", 0),
        task_status_counts.get("failed", 0),
    ]

    open_notification_rows = await db.fetch_all(
        """
        SELECT
            n.*,
            CASE
                WHEN n.entity_type = 'project' THEN n.entity_id
                WHEN n.entity_type = 'task' THEN (
                    SELECT pt.project_id
                    FROM project_tasks pt
                    WHERE pt.task_id = n.entity_id
                    LIMIT 1
                )
                ELSE NULL
            END as project_id
        FROM notifications n
        WHERE n.status = 'pending'
        ORDER BY n.created_at DESC
        LIMIT 5
        """,
    )
    open_notifications = [
        {
            "title": row["title"],
            "message": row["message"],
            "type_label": row["notification_type"].replace("_", " ").title(),
            "delivery_status": row.get("delivery_status", "pending"),
            "created_at": row["created_at"],
            "link_url": f"/dashboard/projects/{row['project_id']}" if row.get("project_id") else None,
        }
        for row in open_notification_rows
    ]

    recent_outcome_rows = await db.fetch_all(
        """
        SELECT
            n.*,
            CASE
                WHEN n.entity_type = 'project' THEN n.entity_id
                WHEN n.entity_type = 'task' THEN (
                    SELECT pt.project_id
                    FROM project_tasks pt
                    WHERE pt.task_id = n.entity_id
                    LIMIT 1
                )
                ELSE NULL
            END as project_id
        FROM notifications n
        WHERE n.notification_type IN ('task_result', 'project_result')
        ORDER BY n.created_at DESC
        LIMIT 5
        """,
    )
    recent_outcomes = [
        {
            "title": row["title"],
            "message": row["message"],
            "type_label": row["notification_type"].replace("_", " ").title(),
            "created_at": row["created_at"],
            "link_url": f"/dashboard/projects/{row['project_id']}" if row.get("project_id") else None,
        }
        for row in recent_outcome_rows
    ]

    return _render_template(
        "dashboard/overview.html",
        request,
        {
            "version": settings.version,
            "pending_count": pending_count,
            "active_projects_count": active_count,
            "planning_projects_count": planning_count,
            "pending_approvals_count": pending_count,
            "urgent_approvals": urgent_count,
            "open_notifications_count": open_notifications_count,
            "saved_health_checks_count": health_snapshot_count,
            "project_status_data": json.dumps(project_status_data),
            "task_status_data": json.dumps(task_status_data),
            "recent_activities": recent_activities,
            "pending_approval_items": pending_approval_items,
            "attention_projects": attention_projects,
            "attention_note": (
                "No saved health checks yet. Health attention appears here after scans run."
                if health_snapshot_count == 0
                else "No projects are currently flagged by saved health checks."
            ),
            "open_notifications": open_notifications,
            "recent_outcomes": recent_outcomes,
        },
    )


@router.get("/projects", response_class=HTMLResponse)
async def projects(
    request: Request,
    db: Database = Depends(get_database),
    status: str | None = None,
) -> Response:
    """Projects list view."""
    settings = _get_settings()

    # Build query
    query = "SELECT * FROM projects WHERE deleted_at IS NULL"
    params = []

    if status:
        query += " AND state = ?"
        params.append(status)

    query += " ORDER BY created_at DESC LIMIT 50"

    projects_data = await db.fetch_all(query, tuple(params))

    # Enrich with task counts and health
    projects = []
    for p in projects_data:
        project_id = p["id"]

        # Get task counts
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

        # Get latest health check
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

    # Get stats
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
    db: Database = Depends(get_database),
) -> Response:
    """Individual project detail view."""
    settings = _get_settings()
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

    # Get tasks for this project
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

    # Get journal entries
    journal = await db.fetch_all(
        """
        SELECT * FROM project_journal_entries
        WHERE project_id = ?
        ORDER BY created_at DESC
        LIMIT 20
        """,
        (project_id,),
    )

    # Get health checks
    health_checks = await db.fetch_all(
        """
        SELECT * FROM latest_project_health
        WHERE project_id = ?
        ORDER BY created_at DESC
        LIMIT 10
        """,
        (project_id,),
    )

    # Get task counts for the project
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

    # Add task counts to project dict for template
    project_data = dict(project)
    project_data["completed_tasks"] = task_counts["completed"] if task_counts else 0
    project_data["total_tasks"] = task_counts["total"] if task_counts else 0

    # Parse success_criteria JSON if present
    if project_data.get("success_criteria"):
        try:
            project_data["success_criteria"] = json.loads(project_data["success_criteria"])
        except (json.JSONDecodeError, TypeError):
            project_data["success_criteria"] = []

    # Get prompt history for this project and its tasks
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

    # Scan project workspace for files
    from cyborg.services.project_service import ProjectService
    project_service = ProjectService(db)
    project_path = await project_service.get_project_path(project_id)

    # Build short-id -> title mapping for task file association
    task_id_map: dict[str, str] = {}
    for t in tasks:
        short = t["id"].replace("-", "")[:8]
        task_id_map[short] = t["title"]

    scanned_files = _scan_project_files(project_path, task_id_map=task_id_map)
    file_categories = _group_files_by_category(scanned_files)

    # Get specs for this project
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
    # Parse JSON fields in specs
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

    # Get pending approval for the latest spec
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
    db: Database = Depends(get_database),
) -> Response:
    """Hard-delete a project and redirect to the projects list."""
    from cyborg.services.project_service import ProjectService
    project_service = ProjectService(db)
    await project_service.delete_project(project_id)
    return Response(
        status_code=303,
        headers={"Location": "/dashboard/projects"},
    )


@router.post("/projects/{project_id}/pause")
async def dashboard_pause_project(
    project_id: str,
    db: Database = Depends(get_database),
) -> Response:
    """Pause a project and redirect back to the project detail page."""
    from cyborg.services.project_service import ProjectService
    project_service = ProjectService(db)
    await project_service.pause_project(project_id)
    return Response(
        status_code=303,
        headers={"Location": f"/dashboard/projects/{project_id}"},
    )


@router.post("/projects/{project_id}/resume")
async def dashboard_resume_project(
    project_id: str,
    background_tasks: BackgroundTasks,
    db: Database = Depends(get_database),
) -> Response:
    """Resume a project and redirect back to the project detail page."""
    from cyborg.services.project_service import ProjectService
    project_service = ProjectService(db)
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
    db: Database = Depends(get_database),
) -> Response:
    """Submit revision guidance for a paused/closed project spec and trigger reasoning."""
    from cyborg.services.project_spec_service import ProjectSpecService

    form = await request.form()
    feedback = str(form.get("feedback", "")).strip()
    allow_aim = form.get("allow_aim_changes") is not None
    allow_criteria = form.get("allow_criteria_changes") is not None

    if not feedback:
        return Response(content="Feedback is required", status_code=400)

    spec_service = ProjectSpecService(db)

    # Transition to PLANNING synchronously so the user sees immediate feedback
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
            pass  # Revision failure is okay — user can retry

    background_tasks.add_task(_run_revision)

    return Response(
        status_code=303,
        headers={"Location": f"/dashboard/projects/{project_id}"},
    )


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_detail(
    task_id: str,
    request: Request,
    db: Database = Depends(get_database),
) -> Response:
    """Individual task detail view with unified activity timeline."""
    settings = _get_settings()
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

    # Parent project for breadcrumb
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

    # Task steps
    steps = await db.fetch_all(
        "SELECT * FROM task_steps WHERE task_id = ? ORDER BY step_number ASC",
        (task_id,),
    )

    # Task files
    task_files = await db.fetch_all(
        "SELECT * FROM task_files WHERE task_id = ? ORDER BY created_at DESC",
        (task_id,),
    )

    # Build unified activity timeline from 4 sources
    timeline: list[dict[str, Any]] = []

    # 1. Task history
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

    # 2. Notifications
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

    # 3. Prompt history
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

    # 4. Approvals
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

    # Sort by timestamp descending
    timeline.sort(key=lambda item: item.get("timestamp") or "", reverse=True)
    timeline = timeline[:100]

    # Pending approval for inline form
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
                    # Resolve media URLs if we have a project_id
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


@router.get("/projects/{project_id}/files/{file_path:path}")
async def serve_project_file(
    project_id: str,
    file_path: str,
    db: Database = Depends(get_database),
) -> Response:
    """Serve a file from the project workspace for browser viewing."""
    from cyborg.services.project_service import ProjectService
    from starlette.responses import FileResponse as StarletteFileResponse
    import mimetypes

    project_service = ProjectService(db)
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


@router.get("/approvals", response_class=HTMLResponse)
async def approvals(
    request: Request,
    db: Database = Depends(get_database),
    type: str | None = None,
) -> Response:
    """Approvals queue view."""
    settings = _get_settings()

    # Build query
    query = """
        SELECT a.*,
               CASE
                   WHEN a.priority = 'urgent' THEN 1
                   WHEN a.priority = 'high' THEN 2
                   WHEN a.priority = 'normal' THEN 3
                   ELSE 4
               END as priority_order
        FROM approvals a
        WHERE a.status = 'pending'
        """
    params = []

    if type:
        query += " AND a.approval_type = ?"
        params.append(type)

    query += " ORDER BY priority_order, a.requested_at ASC"

    approvals_data = await db.fetch_all(query, tuple(params))

    # Get stats
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    stats = await db.fetch_one(
        """
        SELECT
            (SELECT COUNT(*) FROM approvals WHERE status = 'pending') as pending,
            (SELECT COUNT(*) FROM approvals WHERE status = 'approved' AND reviewed_at >= ?) as approved_today,
            (SELECT COUNT(*) FROM approvals WHERE status = 'rejected' AND reviewed_at >= ?) as rejected_today
        """,
        (today_start, today_start),
    )
    avg_response = await db.fetch_one(
        """
        SELECT AVG((julianday(reviewed_at) - julianday(requested_at)) * 24 * 60) as avg_minutes
        FROM approvals
        WHERE status IN ('approved', 'rejected')
          AND reviewed_at IS NOT NULL
          AND requested_at IS NOT NULL
        """,
    )

    approvals = []
    for a in approvals_data:
        proposal_data = None
        proposal_preview = None
        if a.get("proposal_data"):
            try:
                proposal_data = json.loads(a["proposal_data"])
                proposal_preview = json.dumps(proposal_data, indent=2)[:500]
            except:
                proposal_preview = a["proposal_data"][:500] if a["proposal_data"] else None

        input_schema = None
        if a.get("input_schema"):
            try:
                input_schema = json.loads(a["input_schema"])
            except (json.JSONDecodeError, TypeError):
                pass

        # Resolve project_id for task_input approvals to build media URLs
        project_id = None
        approval_metadata = None
        if a.get("metadata"):
            try:
                approval_metadata = json.loads(a["metadata"])
            except (json.JSONDecodeError, TypeError):
                pass

        if a["approval_type"] == "task_input" and a.get("entity_id"):
            # For project-level blocks, entity_id IS the project_id
            if approval_metadata and approval_metadata.get("entity_kind") == "project":
                project_id = a["entity_id"]
            else:
                project_id = await _get_project_id_for_task(db, a["entity_id"])

        # Prefix media paths with full dashboard URLs
        if input_schema and project_id and input_schema.get("type") == "multi_choice":
            for option in input_schema.get("options", []):
                if option.get("image_url"):
                    option["image_url"] = f"/dashboard/projects/{project_id}/files/{option['image_url']}"
                if option.get("audio_url"):
                    option["audio_url"] = f"/dashboard/projects/{project_id}/files/{option['audio_url']}"

        approvals.append({
            "id": a["id"],
            "approval_type": a["approval_type"],
            "type_label": a["approval_type"].replace("_", " ").title(),
            "entity_id": a.get("entity_id"),
            "title": a["title"],
            "description": a.get("description", ""),
            "priority": a.get("priority", "normal"),
            "requested_at": a.get("requested_at"),
            "requested_by": a.get("requested_by"),
            "proposal": proposal_data,
            "proposal_preview": proposal_preview,
            "input_schema": input_schema,
            "project_id": project_id,
            "review_href": _approval_review_href(a["approval_type"], a.get("entity_id"), approval_metadata),
        })

    return _render_template(
        "dashboard/approvals.html",
        request,
        {
            "version": settings.version,
            "approvals": approvals,
            "filter_type": type,
            "pending_count": stats["pending"] if stats else 0,
            "approved_today": stats["approved_today"] if stats else 0,
            "rejected_today": stats["rejected_today"] if stats else 0,
            "avg_response_time": _format_duration_minutes(avg_response["avg_minutes"] if avg_response else None),
        },
    )


@router.post("/approve/{approval_id}", response_class=HTMLResponse)
async def approve_approval(
    approval_id: str,
    request: Request,
    db: Database = Depends(get_database),
) -> Response:
    """Approve an item and update the display."""
    approval = await db.fetch_one(
        "SELECT * FROM approvals WHERE id = ?",
        (approval_id,),
    )
    if not approval:
        return Response(content="Approval not found", status_code=404)

    entity_id = approval["entity_id"]
    approval_type = approval["approval_type"]

    # If this is a project_plan approval, invoke the spec approval service
    if approval_type == "project_plan" and entity_id:
        from cyborg.services.project_spec_service import ProjectSpecService
        from cyborg.models import ProjectSpecApproveRequest

        spec_service = ProjectSpecService(db)
        pending_spec = await db.fetch_one(
            """
            SELECT id FROM project_specs
            WHERE project_id = ? AND status = 'pending_approval'
            ORDER BY version_number DESC LIMIT 1
            """,
            (entity_id,),
        )
        if pending_spec:
            approve_payload = ProjectSpecApproveRequest(approver="dashboard_user")
            await spec_service.approve_spec(str(pending_spec["id"]), approve_payload)
        else:
            # Spec already approved (e.g. re-approving after a reset) — just trigger execution
            from cyborg.models import ProjectState
            project = await db.fetch_one(
                "SELECT state FROM projects WHERE id = ? AND deleted_at IS NULL",
                (entity_id,),
            )
            if project and project["state"] in (
                ProjectState.PLANNING.value,
                ProjectState.PAUSED.value,
                ProjectState.ACTIVE.value,
            ):
                from cyborg.services.project_execution_service import ProjectExecutionService
                execution_service = ProjectExecutionService(db)
                if project["state"] in (ProjectState.PLANNING.value, ProjectState.PAUSED.value, ProjectState.ACTIVE.value):
                    await execution_service.cleanup_old_plan_tasks(entity_id)
                await execution_service.start_project_execution(entity_id)

    # If this is a task_input approval, unblock the entity
    if approval_type == "task_input" and entity_id:
        approval_metadata = json.loads(approval.get("metadata") or "{}")
        is_project_block = approval_metadata.get("entity_kind") == "project"

        if is_project_block:
            # Resume the blocked project
            from cyborg.services.project_service import ProjectService
            from cyborg.services.project_execution_service import ProjectExecutionService
            from cyborg.models import JournalEntryType

            # Add journal entry so reasoning knows the project was unblocked by user
            execution_service = ProjectExecutionService(db)
            await execution_service._add_journal_entry(
                entity_id,
                JournalEntryType.DECISION,
                f"User approved project block (resuming). Block reason: {approval.get('description', '')}",
                {"action": "block_approved", "approval_id": approval_id},
            )

            project_service = ProjectService(db)
            try:
                await project_service.resume_project(entity_id)
                try:
                    await project_service.resume_project_reasoning(entity_id, resumed_from_block=True)
                except Exception:
                    pass
            except Exception:
                pass
        else:
            from cyborg.services.task_service import TaskService
            from cyborg.models import TaskUnblockRequest

            task_service = TaskService(db)
            try:
                await task_service.unblock_task(
                    entity_id,
                    TaskUnblockRequest(notes="Unblocked via dashboard"),
                )
            except Exception:
                pass

    await db.execute(
        """
        UPDATE approvals
        SET status = 'approved',
            reviewed_at = ?,
            reviewed_by = 'dashboard_user'
        WHERE id = ?
        """,
        (datetime.now(timezone.utc).isoformat(), approval_id),
    )

    # Check if redirected from project detail page
    next_url = request.query_params.get("next")
    if next_url:
        return Response(status_code=303, headers={"Location": next_url})

    # Return empty fragment to remove the row (for HTMX from approvals page)
    return Response(content="", status_code=200)


@router.post("/reject/{approval_id}", response_class=HTMLResponse)
async def reject_approval(
    approval_id: str,
    db: Database = Depends(get_database),
) -> Response:
    """Reject an item and update the display."""
    approval = await db.fetch_one(
        "SELECT * FROM approvals WHERE id = ?",
        (approval_id,),
    )
    if not approval:
        return Response(content="Approval not found", status_code=404)

    entity_id = approval["entity_id"]
    approval_type = approval["approval_type"]

    # If this is a project_plan approval, invoke the spec reject service
    if approval_type == "project_plan" and entity_id:
        from cyborg.services.project_spec_service import ProjectSpecService
        from cyborg.models import ProjectSpecRejectRequest

        spec_service = ProjectSpecService(db)
        pending_spec = await db.fetch_one(
            """
            SELECT id FROM project_specs
            WHERE project_id = ? AND status = 'pending_approval'
            ORDER BY version_number DESC LIMIT 1
            """,
            (entity_id,),
        )
        if pending_spec:
            reject_payload = ProjectSpecRejectRequest(feedback="Rejected via dashboard")
            await spec_service.reject_spec(str(pending_spec["id"]), reject_payload)

    # If this is a task_input approval, handle based on entity kind
    if approval_type == "task_input" and entity_id:
        approval_metadata = json.loads(approval.get("metadata") or "{}")
        is_project_block = approval_metadata.get("entity_kind") == "project"

        if is_project_block:
            from cyborg.services.project_service import ProjectService

            project_service = ProjectService(db)
            try:
                await project_service.close_project(
                    entity_id,
                    conclusion="User declined to provide input via dashboard",
                )
            except Exception:
                pass
        else:
            from cyborg.services.task_service import TaskService
            from cyborg.models import TaskUnblockRequest

            task_service = TaskService(db)
            try:
                await task_service.unblock_task(
                    entity_id,
                    TaskUnblockRequest(notes="User declined to provide input via dashboard"),
                )
            except Exception:
                pass

    await db.execute(
        """
        UPDATE approvals
        SET status = 'rejected',
            reviewed_at = ?,
            reviewed_by = 'dashboard_user'
        WHERE id = ?
        """,
        (datetime.now(timezone.utc).isoformat(), approval_id),
    )

    # Return empty fragment to remove the row
    return Response(content="", status_code=200)


@router.post("/specs/{spec_id}/reject-and-revise", response_class=HTMLResponse)
async def reject_and_revise_spec(
    spec_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Database = Depends(get_database),
) -> Response:
    """Reject a spec with feedback and trigger revision reasoning in the background."""
    from cyborg.models import ProjectSpecRejectRequest
    from cyborg.services.project_spec_service import ProjectSpecService

    form = await request.form()
    feedback = str(form.get("feedback", "")).strip()
    allow_aim = form.get("allow_aim_changes") is not None
    allow_criteria = form.get("allow_criteria_changes") is not None

    if not feedback:
        return Response(content="Feedback is required", status_code=400)

    spec_service = ProjectSpecService(db)

    # Reject the spec (fast — DB only)
    reject_payload = ProjectSpecRejectRequest(feedback=feedback)
    await spec_service.reject_spec(spec_id, reject_payload)

    # Mark the approval as rejected
    spec_row = await db.fetch_one("SELECT project_id FROM project_specs WHERE id = ?", (spec_id,))
    project_id = spec_row["project_id"] if spec_row else None
    if project_id:
        await db.execute(
            """
            UPDATE approvals SET status = 'rejected', reviewed_at = ?, reviewed_by = 'dashboard_user'
            WHERE entity_id = ? AND approval_type = 'project_plan' AND status = 'pending'
            """,
            (datetime.now(timezone.utc).isoformat(), project_id),
        )

        # Trigger revision in background (reasoning is slow)
        async def _run_revision():
            try:
                await spec_service.revise_spec_after_rejection(
                    project_id,
                    feedback,
                    allow_aim_changes=allow_aim,
                    allow_criteria_changes=allow_criteria,
                )
            except Exception:
                pass  # Revision failure is okay — project stays in planning for manual re-submission

        background_tasks.add_task(_run_revision)

    # Redirect back to project page immediately
    redirect_id = project_id or ""
    return Response(
        status_code=303,
        headers={"Location": f"/dashboard/projects/{redirect_id}"},
    )


@router.post("/approve/{approval_id}/input", response_class=HTMLResponse)
async def resolve_task_input(
    approval_id: str,
    request: Request,
    db: Database = Depends(get_database),
) -> Response:
    """Resolve a task_input approval with the user's structured input."""
    approval = await db.fetch_one(
        "SELECT * FROM approvals WHERE id = ?",
        (approval_id,),
    )
    if not approval:
        return Response(content="Approval not found", status_code=404)

    if approval["approval_type"] != "task_input":
        return Response(content="Not a task input approval", status_code=400)

    entity_id = approval["entity_id"]

    # Parse the submitted form data
    form = await request.form()
    response_value = form.get("response", "")
    if isinstance(response_value, str) and not response_value.strip():
        return Response(content="Response is required", status_code=400)

    # Determine if multi_choice was used (form sends comma-separated or repeated fields)
    input_schema_raw = approval.get("input_schema")
    input_prompt = ""
    if input_schema_raw:
        try:
            input_schema = json.loads(input_schema_raw)
            input_prompt = input_schema.get("prompt", "")
            if input_schema.get("type") == "multi_choice" and input_schema.get("allow_multiple"):
                # Multi-select: collect all response values
                values = form.getlist("response")
                response_value = [v.strip() for v in values if v.strip()]
        except (json.JSONDecodeError, TypeError):
            pass

    now_iso = datetime.now(timezone.utc).isoformat()
    response_json = json.dumps(response_value if isinstance(response_value, list) else response_value.strip())

    # Update approval with the response
    await db.execute(
        """
        UPDATE approvals
        SET status = 'approved',
            input_response = ?,
            reviewed_at = ?,
            reviewed_by = 'dashboard_user'
        WHERE id = ?
        """,
        (response_json, now_iso, approval_id),
    )

    # Unblock the entity with the user's input
    approval_metadata = json.loads(approval.get("metadata") or "{}")
    is_project_block = approval_metadata.get("entity_kind") == "project"

    response_summary = response_value if isinstance(response_value, str) else ", ".join(response_value)

    if is_project_block:
        # Unblock the project and feed user response into reasoning
        from cyborg.services.project_service import ProjectService
        from cyborg.services.project_execution_service import ProjectExecutionService
        from cyborg.models import JournalEntryType

        # Add journal entry with user's response so reasoning can use it
        execution_service = ProjectExecutionService(db)
        await execution_service._add_journal_entry(
            entity_id,
            JournalEntryType.DECISION,
            f"User response to block: {response_summary}",
            {"user_response": response_summary, "approval_id": approval_id},
        )

        project_service = ProjectService(db)
        try:
            await project_service.resume_project(entity_id)
        except Exception:
            pass
        try:
            await project_service.resume_project_reasoning(entity_id, resumed_from_block=True)
        except Exception:
            pass
    else:
        # Unblock the task with the user's input
        from cyborg.services.task_service import TaskService
        from cyborg.models import TaskUnblockRequest

        task_service = TaskService(db)
        await task_service.unblock_task(
            entity_id,
            TaskUnblockRequest(notes=f"User input received: {response_summary}"),
        )

        # Dispatch input response notification to the task session
        try:
            from cyborg.services.notification_service import NotificationService
            notification_service = NotificationService(db)
            await notification_service.create_task_input_response_notification(
                entity_id,
                response_value if isinstance(response_value, list) else response_value.strip(),
                input_prompt,
                approval_id,
            )
        except Exception:
            logger.exception("Failed to dispatch task_input_response notification for approval %s", approval_id)

    # Return empty fragment to remove the row
    return Response(content="", status_code=200)


@router.post("/tasks/{task_id}/tap")
async def tap_task(
    task_id: str,
    db: Database = Depends(get_database),
) -> Response:
    """Send a nudge to the OpenClaw session working on an active task."""
    from fastapi.responses import JSONResponse

    task = await db.fetch_one(
        "SELECT id, status FROM tasks WHERE id = ? AND deleted_at IS NULL",
        (task_id,),
    )
    if not task:
        return JSONResponse(content={"error": "Task not found"}, status_code=404)

    if task["status"] != TaskStatus.ACTIVE.value:
        return JSONResponse(content={"error": "Task is not active"}, status_code=400)

    from cyborg.services.notification_service import NotificationService
    notification_service = NotificationService(db)
    notification_id = await notification_service.create_task_tap_notification(task_id)

    if notification_id is None:
        return JSONResponse(content={"error": "Task has no delivery route"}, status_code=422)

    return JSONResponse(content={"status": "ok", "task_id": task_id})


@router.get("/logs", response_class=HTMLResponse)
async def logs(
    request: Request,
    db: Database = Depends(get_database),
    level: str | None = None,
    event_type: str | None = None,
    project_id: str | None = None,
) -> Response:
    """Structured log viewer."""
    settings = _get_settings()
    pending_count = await _get_pending_approval_count(db)

    # Check if table exists and has data
    table_exists = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='structured_logs'"
    )

    logs = []
    stats = {"error": 0, "warning": 0, "info": 0, "reasoning": 0}

    if table_exists:
        # Build query
        query = "SELECT * FROM structured_logs"
        conditions = []
        params = []

        if level:
            conditions.append("level = ?")
            params.append(level.upper())
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if project_id:
            conditions.append("project_id = ?")
            params.append(project_id)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY timestamp DESC LIMIT 500"

        rows = await db.fetch_all(query, tuple(params))

        # Format logs for template
        for row in rows:
            logs.append({
                "timestamp": row.get("timestamp", "")[:19].replace("T", " "),
                "level": row["level"],
                "logger": row.get("logger", ""),
                "message": row["message"],
                "event_type": row.get("event_type"),
                "project_id": row.get("project_id"),
                "duration_seconds": row.get("duration_seconds"),
                "extra_data": row.get("extra_data"),
            })

        # Calculate stats from actual data
        stats_rows = await db.fetch_all(
            """
            SELECT
                level,
                event_type,
                COUNT(*) as count
            FROM structured_logs
            WHERE timestamp > datetime('now', '-24 hours')
            GROUP BY level, event_type
            """
        )

        for row in stats_rows:
            level_val = row["level"]
            if level_val == "ERROR":
                stats["error"] += row["count"]
            elif level_val == "WARNING":
                stats["warning"] += row["count"]
            elif level_val == "INFO":
                stats["info"] += row["count"]
            if row.get("event_type") == "reasoning_request":
                stats["reasoning"] += row["count"]

    # Show empty state message if no logs
    if not logs:
        logs = [{
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "level": "INFO",
            "logger": "cyborg.dashboard",
            "message": "No logs yet. Logs will appear here once structured logging captures events.",
            "event_type": None,
            "project_id": None,
            "duration_seconds": None,
            "extra_data": None,
        }]

    return _render_template(
        "dashboard/logs.html",
        request,
        {
            "version": settings.version,
            "logs": logs,
            "stats": stats,
            "last_log_time": datetime.now(timezone.utc).isoformat(),
            "pending_count": pending_count,
        },
    )


@router.get("/health", response_class=HTMLResponse)
async def health(
    request: Request,
    db: Database = Depends(get_database),
) -> Response:
    """Health monitoring view."""
    settings = _get_settings()
    pending_count = await _get_pending_approval_count(db)

    # Get health stats
    health_stats = await db.fetch_one(
        """
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN h.health_score >= 0.8 THEN 1 ELSE 0 END) as healthy,
            SUM(CASE WHEN h.health_score >= 0.5 AND h.health_score < 0.8 THEN 1 ELSE 0 END) as medium_risk,
            SUM(CASE WHEN h.health_score < 0.5 OR h.risk_level IN ('high', 'critical') THEN 1 ELSE 0 END) as high_risk,
            AVG(h.health_score) as avg_health
        FROM latest_project_health h
        INNER JOIN projects p ON p.id = h.project_id
        WHERE p.deleted_at IS NULL
        """,
    )

    # Get at-risk projects
    at_risk = await db.fetch_all(
        """
        SELECT p.*, h.health_score, h.risk_level, h.created_at as last_checked
        FROM projects p
        INNER JOIN latest_project_health h ON h.project_id = p.id
        WHERE p.deleted_at IS NULL
          AND (h.health_score < 0.5 OR h.risk_level IN ('high', 'critical'))
        ORDER BY h.health_score ASC
        LIMIT 20
        """,
    )

    # Calculate additional stats for at-risk projects
    for project in at_risk:
        project_id = project["id"]
        # Get blocked/failed task counts
        task_stats = await db.fetch_one(
            """
            SELECT
                SUM(CASE WHEN t.status = 'blocked' THEN 1 ELSE 0 END) as blocked_tasks,
                SUM(CASE WHEN t.status = 'failed' THEN 1 ELSE 0 END) as failed_tasks
            FROM tasks t
            INNER JOIN project_tasks pt ON pt.task_id = t.id
            WHERE pt.project_id = ? AND t.deleted_at IS NULL
            """,
            (project_id,),
        )
        project["blocked_tasks"] = task_stats["blocked_tasks"] if task_stats else 0
        project["failed_tasks"] = task_stats["failed_tasks"] if task_stats else 0

    healthy_count = health_stats["healthy"] if health_stats else 0
    medium_risk_count = health_stats["medium_risk"] if health_stats else 0
    high_risk_count = health_stats["high_risk"] if health_stats else 0
    avg_health = health_stats["avg_health"] if health_stats else 0
    avg_health_score = int(avg_health * 100) if avg_health else 0

    return _render_template(
        "dashboard/health.html",
        request,
        {
            "version": settings.version,
            "at_risk_projects": at_risk,
            "pending_count": pending_count,
            "healthy_count": healthy_count,
            "medium_risk_count": medium_risk_count,
            "high_risk_count": high_risk_count,
            "avg_health_score": avg_health_score,
        },
    )


# ============================================================================
# SSE Endpoints for Real-time Updates
# ============================================================================

@router.get("/events")
@router.get("/logs/stream")
async def dashboard_events(request: Request) -> StreamingResponse:
    """Server-Sent Events endpoint for real-time dashboard updates."""

    async def event_stream():
        """Yield SSE events for dashboard updates."""
        while True:
            # In a real implementation, this would check for new logs,
            # status changes, etc. and push them to connected clients.
            yield f"event: message\ndata: {json.dumps({'type': 'heartbeat', 'timestamp': datetime.now(timezone.utc).isoformat()})}\n\n"
            try:
                await asyncio.sleep(30)  # Heartbeat every 30 seconds
            except asyncio.CancelledError:
                break

    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
    )


# ============================================================================
# Chart Data API Endpoints
# ============================================================================

@router.get("/api/chart/project-status-distribution")
async def chart_project_status(db: Database = Depends(get_database)) -> dict[str, Any]:
    """Get project status distribution for charts."""
    result = await db.fetch_all(
        """
        SELECT state, COUNT(*) as count
        FROM projects
        WHERE deleted_at IS NULL
        GROUP BY state
        """,
    )

    return {
        "labels": [r["state"] for r in result],
        "data": [r["count"] for r in result],
    }


@router.get("/api/chart/reasoning-latency")
async def chart_reasoning_latency(
    hours: int = 24,
    db: Database = Depends(get_database),
) -> dict[str, Any]:
    """Get reasoning latency over time for charts."""
    now = datetime.now(timezone.utc)
    labels = []
    data = []

    # Get reasoning requests from structured logs
    for i in range(hours):
        t = now - timedelta(hours=(hours - 1 - i))
        hour_label = t.strftime("%H:00")

        # Get duration for reasoning requests in this hour
        result = await db.fetch_one(
            """
            SELECT AVG(duration_seconds) as avg_duration,
                   COUNT(*) as count
            FROM structured_logs
            WHERE event_type = 'reasoning_request'
              AND timestamp >= ?
              AND timestamp < ?
            """,
            (t.strftime("%Y-%m-%dT%H:00:00"), (t + timedelta(hours=1)).strftime("%Y-%m-%dT%H:00:00"))
        )

        if result:
            avg_ms = int((result["avg_duration"] or 0) * 1000)
            data.append(avg_ms if result["count"] > 0 else 0)

        labels.append(hour_label)

    return {"labels": labels, "data": data}


@router.get("/api/chart/task-breakdown")
async def chart_task_breakdown(db: Database = Depends(get_database)) -> dict[str, Any]:
    """Get task status breakdown for charts."""
    result = await db.fetch_all(
        """
        SELECT status, COUNT(*) as count
        FROM tasks
        WHERE deleted_at IS NULL
        GROUP BY status
        """,
    )

    return {
        "labels": [r["status"] for r in result],
        "data": [r["count"] for r in result],
    }


@router.get("/api/chart/health-distribution")
async def chart_health_distribution(db: Database = Depends(get_database)) -> dict[str, Any]:
    """Get health distribution for charts."""
    result = await db.fetch_all(
        """
        SELECT
            CASE
                WHEN health_score >= 0.8 THEN 'low'
                WHEN health_score >= 0.5 THEN 'medium'
                WHEN health_score >= 0.3 OR (health_score < 0.5 AND health_score >= 0) THEN 'high'
                WHEN health_score < 0.3 OR health_score IS NULL THEN 'critical'
                ELSE 'unknown'
            END as risk_level,
            COUNT(*) as count
        FROM latest_project_health
        INNER JOIN projects p ON p.id = h.project_id
        WHERE p.deleted_at IS NULL
        GROUP BY risk_level
        """,
    )

    return {
        "labels": ["Low Risk", "Medium Risk", "High Risk", "Critical"],
        "data": [0, 0, 0, 0],
    }


@router.get("/api/chart/log-events")
async def chart_log_events(db: Database = Depends(get_database), hours: int = 24) -> dict[str, Any]:
    """Get log event counts for charts."""
    result = await db.fetch_all(
        """
        SELECT
            CASE
                WHEN event_type = 'reasoning_request' THEN 'reasoning'
                WHEN event_type = 'autonomy_decision' THEN 'autonomy'
                WHEN event_type = 'health_check' THEN 'health'
                WHEN event_type = 'function_call' THEN 'function'
                WHEN event_type = 'error' THEN 'error'
                ELSE 'other'
            END as category,
            COUNT(*) as count
        FROM structured_logs
        WHERE timestamp > datetime('now', '-' || str(hours) || ' hours')
        GROUP BY category
        """,
    )

    categories = [r["category"] for r in result]
    return {
        "labels": categories,
        "data": [r["count"] for r in result],
    }


# ============================================================================
# Helper Functions
# ============================================================================

def _render_template(template_name: str, request: Request, context: dict[str, Any]) -> HTMLResponse:
    """Render a Jinja2 template with the given context."""
    from fastapi.templating import Jinja2Templates
    from pathlib import Path
    from jinja2 import Environment, FileSystemLoader

    # Get the templates directory relative to the package root
    templates_dir = Path(__file__).parent.parent / "templates"

    # Create a custom Jinja2 environment with filters
    env = Environment(loader=FileSystemLoader(str(templates_dir)))

    # Register custom filters
    env.filters['relative_time'] = _format_relative_time
    env.filters['file_size'] = _format_file_size
    env.filters['file_icon'] = _file_icon

    # Add request to context
    context["request"] = request

    # Add settings
    settings = _get_settings()
    context.setdefault("version", settings.version)
    context.setdefault("pending_count", 0)

    template = env.get_template(template_name)
    response = HTMLResponse(content=template.render(context))

    # Set dashboard secret cookie so form submissions are authenticated
    if settings.dashboard_secret_configured:
        response.set_cookie(
            key="cyborg_dashboard_secret",
            value=settings.dashboard_secret,
            httponly=True,
            samesite="strict",
        )

    return response


def _format_time(iso_string: str | None) -> str:
    """Format an ISO timestamp for display."""
    if not iso_string:
        return "--:--:--"
    try:
        dt = datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%H:%M:%S")
    except:
        return "--:--:--"


def _format_relative_time(iso_string: str | None) -> str:
    """Format an ISO timestamp as relative time."""
    if not iso_string:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        if delta.days > 0:
            return f"{delta.days}d ago"
        elif delta.seconds >= 3600:
            hours = delta.seconds // 3600
            return f"{hours}h ago"
        elif delta.seconds >= 60:
            minutes = delta.seconds // 60
            return f"{minutes}m ago"
        else:
            return "just now"
    except:
        return "unknown"


def _format_duration_minutes(avg_minutes: float | None) -> str:
    """Format an average response time in minutes."""
    if avg_minutes is None:
        return "--"
    total_minutes = int(round(avg_minutes))
    if total_minutes < 60:
        return f"{total_minutes}m"
    hours, minutes = divmod(total_minutes, 60)
    if minutes == 0:
        return f"{hours}h"
    return f"{hours}h {minutes}m"


def _format_file_size(size_bytes: int | None) -> str:
    """Format a byte count as a human-readable size."""
    if size_bytes is None:
        return "--"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


def _file_icon(filename: str) -> str:
    """Return a small text icon based on file extension."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    icons = {
        "md": "\U0001f4c4", "txt": "\U0001f4c4",
        "png": "\U0001f5bc", "jpg": "\U0001f5bc", "jpeg": "\U0001f5bc",
        "webp": "\U0001f5bc", "gif": "\U0001f5bc",
        "mp3": "\U0001f3b5", "wav": "\U0001f3b5", "flac": "\U0001f3b5",
        "py": "\U0001f40d", "sh": "\u2699",
        "json": "\U0001f4cb", "csv": "\U0001f4ca",
        "pdf": "\U0001f4d5",
    }
    return icons.get(ext, "\U0001f4ce")


_SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".mypy_cache"}


def _scan_project_files(
    project_path: Path,
    *,
    task_id_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Scan the project workspace for files, returning entries for the template."""
    from pathlib import Path as _Path

    workspace_root = project_path.resolve()
    if not workspace_root.exists():
        return []

    files: list[dict[str, Any]] = []
    for child in sorted(workspace_root.rglob("*")):
        if not child.is_file():
            continue
        rel_parts = child.relative_to(workspace_root).parts
        if any(part.startswith(".") or part in _SKIP_DIRS for part in rel_parts):
            continue

        relative = str(child.relative_to(workspace_root))
        stat = child.stat()
        category = rel_parts[0] if len(rel_parts) > 1 else "root"

        entry: dict[str, Any] = {
            "name": child.name,
            "relative_path": relative,
            "category": category,
            "size_bytes": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        }

        # Annotate task files with their task title
        if category == "tasks" and len(rel_parts) >= 3 and task_id_map:
            short_id = rel_parts[1]
            if short_id in task_id_map:
                entry["task_short_id"] = short_id
                entry["task_title"] = task_id_map[short_id]

        files.append(entry)

    return files


def _group_files_by_category(files: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group scanned files by their category (first path segment)."""
    categories: dict[str, list[dict[str, Any]]] = {}
    for f in files:
        categories.setdefault(f["category"], []).append(f)
    return categories
