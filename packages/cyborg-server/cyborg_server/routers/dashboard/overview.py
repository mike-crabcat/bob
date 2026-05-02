"""Dashboard overview route."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse

from cyborg_server.database import Database
from cyborg_server.dependencies import get_database
from cyborg_server.models import ProjectState

from ._helpers import _get_pending_approval_count, _get_settings, _render_template

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def overview(
    request: Request,
    db: Database = Depends(get_database),
) -> Response:
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
    from ._helpers import _approval_review_href, _format_time

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

    active_dispatch_rows = await db.fetch_all(
        """
        SELECT d.*,
               t.title AS task_title,
               p.title AS project_title
        FROM dispatches d
        LEFT JOIN tasks t ON t.id = d.task_id AND t.deleted_at IS NULL
        LEFT JOIN projects p ON p.id = d.project_id AND p.deleted_at IS NULL
        WHERE d.status = 'active'
        ORDER BY d.dispatched_at ASC
        LIMIT 20
        """,
    )
    active_dispatches = [
        {
            "id": row["id"],
            "notification_type": row["notification_type"],
            "session_key": row["session_key"],
            "task_id": row["task_id"],
            "task_title": row["task_title"],
            "project_title": row["project_title"],
            "dispatched_at": row["dispatched_at"],
            "tap_count": row["tap_count"],
        }
        for row in active_dispatch_rows
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
            "active_dispatches": active_dispatches,
        },
    )
