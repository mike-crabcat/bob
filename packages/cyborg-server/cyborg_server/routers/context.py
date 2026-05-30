"""HTTP routes for condensed context views."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends

from cyborg_server.dependencies import get_database
from cyborg_server.database import Database
from cyborg_server.models import (
    ContextCalendarResponse,
    ContextSummaryResponse,
    EventContextItem,
)
from cyborg_server.services.base import utcnow


router = APIRouter(prefix="/api/v1/context", tags=["context"])


@router.get("/summary", response_model=ContextSummaryResponse)
async def context_summary(database: Database = Depends(get_database)) -> ContextSummaryResponse:
    generated_at = utcnow()
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
        upcoming_events=[EventContextItem.model_validate(row) for row in event_rows],
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
