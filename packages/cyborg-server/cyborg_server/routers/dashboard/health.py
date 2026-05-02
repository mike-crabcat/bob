"""Dashboard health route."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse

from cyborg_server.database import Database
from cyborg_server.dependencies import get_database

from ._helpers import _get_pending_approval_count, _get_settings, _render_template

router = APIRouter()


@router.get("/health", response_class=HTMLResponse)
async def health(
    request: Request,
    db: Database = Depends(get_database),
) -> Response:
    settings = _get_settings()
    pending_count = await _get_pending_approval_count(db)

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

    for project in at_risk:
        project_id = project["id"]
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
