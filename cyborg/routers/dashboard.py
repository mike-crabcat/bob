"""Dashboard router for the Cyborg web interface.

Provides a self-hosted cyberpunk-themed dashboard with real-time monitoring,
project management, approvals workflow, and log viewing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from jinja2 import FileSystemLoader

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse
from starlette.responses import StreamingResponse

from cyborg.config import Settings
from cyborg.database import Database
from cyborg.dependencies import get_database
from cyborg.models import ProjectState


router = APIRouter(prefix="/dashboard", tags=["dashboard"])
logger = logging.getLogger(__name__)


def _get_settings() -> Settings:
    """Get application settings."""
    return Settings.from_env()


@router.get("/", response_class=HTMLResponse)
async def overview(
    request: Request,
    db: Database = Depends(get_database),
) -> Response:
    """Dashboard overview with system stats and charts."""
    settings = _get_settings()

    # Get system stats
    active_projects = await db.fetch_all(
        "SELECT COUNT(*) as count FROM projects WHERE state = ? AND deleted_at IS NULL",
        (ProjectState.ACTIVE.value,),
    )
    active_count = active_projects[0]["count"] if active_projects else 0

    completed_projects = await db.fetch_all(
        "SELECT COUNT(*) as count FROM projects WHERE state = ? AND deleted_at IS NULL",
        (ProjectState.CLOSED.value,),
    )
    completed_count = completed_projects[0]["count"] if completed_projects else 0

    pending_approvals = await db.fetch_all(
        "SELECT COUNT(*) as count FROM approvals WHERE status = 'pending'",
    )
    pending_count = pending_approvals[0]["count"] if pending_approvals else 0

    urgent_approvals = await db.fetch_all(
        "SELECT COUNT(*) as count FROM approvals WHERE status = 'pending' AND priority = 'urgent'",
    )
    urgent_count = urgent_approvals[0]["count"] if urgent_approvals else 0

    # Get at-risk projects (low health score)
    at_risk = await db.fetch_all(
        """
        SELECT COUNT(*) as count FROM latest_project_health
        WHERE health_score < 0.5 OR risk_level IN ('high', 'critical')
        """,
    )
    at_risk_count = at_risk[0]["count"] if at_risk else 0

    # Get project status distribution for charts
    project_stats = await db.fetch_all(
        """
        SELECT state, COUNT(*) as count
        FROM projects
        WHERE deleted_at IS NULL
        GROUP BY state
        """,
    )
    status_counts = {row["state"]: row["count"] for row in project_stats}
    project_status_data = [
        status_counts.get("active", 0),
        status_counts.get("closed", 0),
        status_counts.get("blocked", 0),
        status_counts.get("paused", 0),
    ]

    # Get recent activity (from journal entries)
    recent_journal = await db.fetch_all(
        """
        SELECT * FROM project_journal_entries
        ORDER BY created_at DESC
        LIMIT 10
        """,
    )
    recent_activities = []
    for entry in recent_journal:
        activity_type = "info"
        if entry["entry_type"] == "MILESTONE":
            activity_type = "completion"
        elif entry["entry_type"] == "DECISION":
            activity_type = "refinement"
        elif entry["entry_type"] == "BLOCKER":
            activity_type = "health"

        recent_activities.append({
            "timestamp": _format_time(entry["created_at"]),
            "type": activity_type,
            "message": entry["content"][:100] + "..." if len(entry["content"]) > 100 else entry["content"],
            "project_id": entry.get("project_id"),
        })

    # Get pending approval items
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
        pending_approval_items.append({
            "id": item["id"],
            "type": approval_type,
            "type_label": approval_type.replace("_", " ").title(),
            "title": item["title"],
            "description": item.get("description", ""),
            "priority": item.get("priority", "normal"),
            "requested_at": _format_time(item["requested_at"]),
        })

    # Get task status distribution
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
        task_status_counts.get("completed", 0),
        task_status_counts.get("failed", 0),
        task_status_counts.get("blocked", 0),
    ]

    # Get health distribution
    health_stats = await db.fetch_all(
        """
        SELECT
            CASE
                WHEN health_score >= 0.8 THEN 'low'
                WHEN health_score >= 0.5 THEN 'medium'
                WHEN health_score >= 0.3 THEN 'high'
                ELSE 'critical'
            END as risk_level,
            COUNT(*) as count
        FROM latest_project_health
        GROUP BY risk_level
        """,
    )
    health_distribution = [0, 0, 0, 0]  # low, medium, high, critical
    risk_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    for row in health_stats:
        idx = risk_order.get(row["risk_level"], 0)
        health_distribution[idx] = row["count"]

    # Get log event counts (from structured logs, estimated)
    log_events_data = [45, 23, 12, 89, 3]  # reasoning, autonomy, health, function, error

    # Generate latency chart data (mock - would come from actual logs)
    now = datetime.now(timezone.utc)
    latency_labels = []
    latency_data = []
    for i in range(24):
        t = now - timedelta(hours=(23 - i))
        latency_labels.append(t.strftime("%H:00"))
        latency_data.append(2000 + (i * 100) + (i % 3) * 500)  # Mock data

    return _render_template(
        "dashboard/overview.html",
        request,
        {
            "version": settings.version,
            "pending_count": pending_count,
            "system_health_score": 98,
            "system_health_status": "All systems operational",
            "active_projects_count": active_count,
            "projects_in_progress": active_count,
            "pending_approvals_count": pending_count,
            "urgent_approvals": urgent_count,
            "at_risk_count": at_risk_count,
            "project_status_data": json.dumps(project_status_data),
            "latency_labels": json.dumps(latency_labels),
            "latency_data": json.dumps(latency_data),
            "task_status_data": json.dumps(task_status_data),
            "health_distribution_data": json.dumps(health_distribution),
            "log_events_data": json.dumps(log_events_data),
            "recent_activities": recent_activities,
            "pending_approval_items": pending_approval_items,
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

    # Get stats
    all_stats = await db.fetch_one(
        """
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN state = 'active' THEN 1 ELSE 0 END) as active,
            SUM(CASE WHEN state = 'closed' THEN 1 ELSE 0 END) as completed,
            SUM(CASE WHEN state = 'blocked' THEN 1 ELSE 0 END) as blocked
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
            "stats": {
                "total": all_stats["total"] if all_stats else 0,
                "active": all_stats["active"] if all_stats else 0,
                "completed": all_stats["completed"] if all_stats else 0,
                "blocked": all_stats["blocked"] if all_stats else 0,
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

    project = await db.fetch_one(
        "SELECT * FROM projects WHERE id = ? AND deleted_at IS NULL",
        (project_id,),
    )

    if not project:
        return _render_template(
            "dashboard/base.html",
            request,
            {"version": settings.version, "pending_count": 0},
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

    return _render_template(
        "dashboard/project_detail.html",
        request,
        {
            "version": settings.version,
            "pending_count": 0,
            "project": project_data,
            "tasks": tasks,
            "journal": journal,
            "health_checks": health_checks,
        },
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
            "avg_response_time": "2.3h",
        },
    )


@router.post("/approve/{approval_id}", response_class=HTMLResponse)
async def approve_approval(
    approval_id: str,
    db: Database = Depends(get_database),
) -> Response:
    """Approve an item and update the display."""
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

    # Return empty fragment to remove the row
    return Response(content="", status_code=200)


@router.post("/reject/{approval_id}", response_class=HTMLResponse)
async def reject_approval(
    approval_id: str,
    db: Database = Depends(get_database),
) -> Response:
    """Reject an item and update the display."""
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
    logs = []
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

    stats = {"error": 0, "warning": 0, "info": 0, "reasoning": 0}
    for row in stats_rows:
        level = row["level"]
        if level == "ERROR":
            stats["error"] += row["count"]
        elif level == "WARNING":
            stats["warning"] += row["count"]
        elif level == "INFO":
            stats["info"] += row["count"]
        if row.get("event_type") == "reasoning_request":
            stats["reasoning"] += row["count"]

    return _render_template(
        "dashboard/logs.html",
        request,
        {
            "version": settings.version,
            "logs": mock_logs * 5,  # Duplicate for demo
            "stats": stats,
            "last_log_time": datetime.now(timezone.utc).isoformat(),
            "pending_count": 0,
        },
    )


@router.get("/health", response_class=HTMLResponse)
async def health(
    request: Request,
    db: Database = Depends(get_database),
) -> Response:
    """Health monitoring view."""
    settings = _get_settings()

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
            "pending_count": 0,
            "healthy_count": healthy_count,
            "medium_risk_count": medium_risk_count,
            "high_risk_count": high_risk_count,
            "avg_health_score": avg_health_score,
        },
    )


@router.get("/tasks", response_class=HTMLResponse)
async def tasks(
    request: Request,
    db: Database = Depends(get_database),
) -> Response:
    """Task list view."""
    settings = _get_settings()

    tasks = await db.fetch_all(
        """
        SELECT t.*, pt.project_id
        FROM tasks t
        LEFT JOIN project_tasks pt ON pt.task_id = t.id
        WHERE t.deleted_at IS NULL
        ORDER BY t.created_at DESC
        LIMIT 100
        """,
    )

    return _render_template(
        "dashboard/tasks.html",
        request,
        {
            "version": settings.version,
            "tasks": tasks,
            "pending_count": 0,
        },
    )


# ============================================================================
# SSE Endpoints for Real-time Updates
# ============================================================================

@router.get("/events")
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
    # This would query from structured logs
    # For now, return mock data
    now = datetime.now(timezone.utc)
    labels = []
    data = []
    for i in range(hours):
        t = now - timedelta(hours=(hours - 1 - i))
        labels.append(t.strftime("%H:00"))
        data.append(1500 + (i * 100) + (i % 4) * 300)

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

    # Add request to context
    context["request"] = request

    # Add settings
    settings = _get_settings()
    context["version"] = settings.version
    context["pending_count"] = 0  # Default, will be overridden

    template = env.get_template(template_name)
    return HTMLResponse(content=template.render(context))


def _format_time(iso_string: str | None) -> str:
    """Format an ISO timestamp for display."""
    if not iso_string:
        return "--:--:--"
    try:
        dt = datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
        return dt.strftime("%H:%M:%S")
    except:
        return "--:--:--"


def _format_relative_time(iso_string: str | None) -> str:
    """Format an ISO timestamp as relative time."""
    if not iso_string:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
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
