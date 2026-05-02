"""Dashboard chart data API routes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends

from cyborg_server.database import Database
from cyborg_server.dependencies import get_database

router = APIRouter()


@router.get("/api/chart/project-status-distribution")
async def chart_project_status(db: Database = Depends(get_database)) -> dict[str, Any]:
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
    now = datetime.now(timezone.utc)
    labels = []
    data = []

    for i in range(hours):
        t = now - timedelta(hours=(hours - 1 - i))
        hour_label = t.strftime("%H:00")

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
    await db.fetch_all(
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
