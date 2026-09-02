"""HTTP routes for condensed context views."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends

from server.dependencies import get_database
from server.database import Database
from server.models import (
    ContextCalendarResponse,
    ContextSummaryResponse,
    EventContextItem,
)
from server.services.base import utcnow
from server.services.calendar_service import CalendarService


router = APIRouter(prefix="/api/v1/context", tags=["context"])


@router.get("/summary", response_model=ContextSummaryResponse)
async def context_summary(database: Database = Depends(get_database)) -> ContextSummaryResponse:
    generated_at = utcnow()
    event_rows = await CalendarService.from_db(database).upcoming_context_events(
        start_iso=generated_at.isoformat(),
        end_iso=(generated_at + timedelta(days=14)).isoformat(),
        limit=8,
    )
    return ContextSummaryResponse(
        generated_at=generated_at,
        upcoming_events=[EventContextItem.model_validate(row) for row in event_rows],
    )


@router.get("/calendar", response_model=ContextCalendarResponse)
async def context_calendar(database: Database = Depends(get_database)) -> ContextCalendarResponse:
    generated_at = utcnow()
    rows = await CalendarService.from_db(database).upcoming_context_events(
        start_iso=generated_at.isoformat(), limit=12,
    )
    return ContextCalendarResponse(
        generated_at=generated_at,
        events=[EventContextItem.model_validate(row) for row in rows],
    )
