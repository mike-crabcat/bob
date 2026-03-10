"""HTTP routes for Bob's condensed context views."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends

from cyborg.dependencies import get_database
from cyborg.database import Database
from cyborg.models import (
    ContextCalendarResponse,
    ContextProjectsResponse,
    ContextSummaryResponse,
    ContextTasksResponse,
    EventContextItem,
    ProjectContextItem,
    TaskContextItem,
)
from cyborg.services.base import utcnow


router = APIRouter(prefix="/api/v1/context", tags=["context"])


@router.get("/summary", response_model=ContextSummaryResponse)
async def context_summary(database: Database = Depends(get_database)) -> ContextSummaryResponse:
    generated_at = utcnow()
    task_counts_rows = await database.fetch_all(
        "SELECT status, COUNT(*) AS count FROM tasks WHERE deleted_at IS NULL GROUP BY status"
    )
    project_counts_rows = await database.fetch_all(
        "SELECT state, COUNT(*) AS count FROM projects WHERE deleted_at IS NULL GROUP BY state"
    )
    active_task_rows = await database.fetch_all(
        """
        SELECT id, title, status, priority, updated_at
        FROM tasks
        WHERE deleted_at IS NULL AND status IN ('active', 'pending', 'paused')
        ORDER BY CASE priority
            WHEN 'critical' THEN 1
            WHEN 'high' THEN 2
            WHEN 'medium' THEN 3
            ELSE 4
        END, updated_at DESC
        LIMIT 8
        """
    )
    active_project_rows = await database.fetch_all(
        """
        SELECT id, title, state, aim
        FROM projects
        WHERE deleted_at IS NULL AND state IN ('active', 'planning', 'paused')
        ORDER BY created_at DESC
        LIMIT 8
        """
    )
    event_rows = await database.fetch_all(
        """
        SELECT e.id, e.title, e.start_time, e.end_time, e.timezone, e.status, e.venue, e.calendar_id
        FROM events AS e
        INNER JOIN calendars AS c ON c.id = e.calendar_id
        WHERE e.deleted_at IS NULL AND c.deleted_at IS NULL AND e.start_time >= ? AND e.start_time <= ?
        ORDER BY e.start_time ASC
        LIMIT 8
        """,
        (generated_at.isoformat(), (generated_at + timedelta(days=14)).isoformat()),
    )
    return ContextSummaryResponse(
        generated_at=generated_at,
        task_counts={row["status"]: row["count"] for row in task_counts_rows},
        project_counts={row["state"]: row["count"] for row in project_counts_rows},
        upcoming_events=[EventContextItem.model_validate(row) for row in event_rows],
        active_tasks=[TaskContextItem.model_validate(row) for row in active_task_rows],
        active_projects=[ProjectContextItem.model_validate(row) for row in active_project_rows],
    )


@router.get("/tasks", response_model=ContextTasksResponse)
async def context_tasks(database: Database = Depends(get_database)) -> ContextTasksResponse:
    generated_at = utcnow()
    rows = await database.fetch_all(
        """
        SELECT id, title, status, priority, updated_at
        FROM tasks
        WHERE deleted_at IS NULL AND status IN ('active', 'pending', 'paused')
        ORDER BY CASE priority
            WHEN 'critical' THEN 1
            WHEN 'high' THEN 2
            WHEN 'medium' THEN 3
            ELSE 4
        END, updated_at DESC
        LIMIT 12
        """
    )
    return ContextTasksResponse(
        generated_at=generated_at,
        tasks=[TaskContextItem.model_validate(row) for row in rows],
    )


@router.get("/projects", response_model=ContextProjectsResponse)
async def context_projects(database: Database = Depends(get_database)) -> ContextProjectsResponse:
    generated_at = utcnow()
    rows = await database.fetch_all(
        """
        SELECT id, title, state, aim
        FROM projects
        WHERE deleted_at IS NULL AND state IN ('active', 'planning', 'paused')
        ORDER BY created_at DESC
        LIMIT 12
        """
    )
    return ContextProjectsResponse(
        generated_at=generated_at,
        projects=[ProjectContextItem.model_validate(row) for row in rows],
    )


@router.get("/calendar", response_model=ContextCalendarResponse)
async def context_calendar(database: Database = Depends(get_database)) -> ContextCalendarResponse:
    generated_at = utcnow()
    rows = await database.fetch_all(
        """
        SELECT e.id, e.title, e.start_time, e.end_time, e.timezone, e.status, e.venue, e.calendar_id
        FROM events AS e
        INNER JOIN calendars AS c ON c.id = e.calendar_id
        WHERE e.deleted_at IS NULL AND c.deleted_at IS NULL AND e.start_time >= ?
        ORDER BY e.start_time ASC
        LIMIT 12
        """,
        (generated_at.isoformat(),),
    )
    return ContextCalendarResponse(
        generated_at=generated_at,
        events=[EventContextItem.model_validate(row) for row in rows],
    )
