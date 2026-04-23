"""HTTP routes for calendars, events, and recipients."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from cyborg_server.dependencies import get_calendar_service
from cyborg_server.models import (
    CalendarCreate,
    CalendarResponse,
    CalendarUpdate,
    EventCreate,
    EventRecipientCreate,
    EventRecipientResponse,
    EventRecipientUpdate,
    EventResponse,
    EventUpdate,
)
from cyborg_server.services.calendar_service import CalendarService


router = APIRouter(prefix="/api/v1", tags=["calendars"])


@router.get("/calendars", response_model=list[CalendarResponse])
async def list_calendars(service: CalendarService = Depends(get_calendar_service)) -> list[CalendarResponse]:
    return await service.list_calendars()


@router.post("/calendars", response_model=CalendarResponse, status_code=status.HTTP_201_CREATED)
async def create_calendar(
    payload: CalendarCreate,
    service: CalendarService = Depends(get_calendar_service),
) -> CalendarResponse:
    return await service.create_calendar(payload)


@router.get("/calendars/{calendar_id}", response_model=CalendarResponse)
async def get_calendar(calendar_id: UUID, service: CalendarService = Depends(get_calendar_service)) -> CalendarResponse:
    return await service.get_calendar(str(calendar_id))


@router.put("/calendars/{calendar_id}", response_model=CalendarResponse)
async def update_calendar(
    calendar_id: UUID,
    payload: CalendarUpdate,
    service: CalendarService = Depends(get_calendar_service),
) -> CalendarResponse:
    return await service.update_calendar(str(calendar_id), payload)


@router.delete("/calendars/{calendar_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_calendar(calendar_id: UUID, service: CalendarService = Depends(get_calendar_service)) -> Response:
    await service.delete_calendar(str(calendar_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/events", response_model=list[EventResponse], tags=["events"])
async def list_events(
    calendar_id: UUID | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    service: CalendarService = Depends(get_calendar_service),
) -> list[EventResponse]:
    return await service.list_events(
        calendar_id=str(calendar_id) if calendar_id else None,
        date_from=date_from.isoformat() if date_from else None,
        date_to=date_to.isoformat() if date_to else None,
    )


@router.post("/events", response_model=EventResponse, status_code=status.HTTP_201_CREATED, tags=["events"])
async def create_event(payload: EventCreate, service: CalendarService = Depends(get_calendar_service)) -> EventResponse:
    return await service.create_event(payload)


@router.get("/events/{event_id}", response_model=EventResponse, tags=["events"])
async def get_event(event_id: UUID, service: CalendarService = Depends(get_calendar_service)) -> EventResponse:
    return await service.get_event(str(event_id))


@router.put("/events/{event_id}", response_model=EventResponse, tags=["events"])
async def update_event(
    event_id: UUID,
    payload: EventUpdate,
    service: CalendarService = Depends(get_calendar_service),
) -> EventResponse:
    return await service.update_event(str(event_id), payload)


@router.delete("/events/{event_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["events"])
async def delete_event(event_id: UUID, service: CalendarService = Depends(get_calendar_service)) -> Response:
    await service.delete_event(str(event_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/events/{event_id}/confirm", response_model=EventResponse, tags=["events"])
async def confirm_event(event_id: UUID, service: CalendarService = Depends(get_calendar_service)) -> EventResponse:
    return await service.confirm_event(str(event_id))


@router.post("/events/{event_id}/cancel", response_model=EventResponse, tags=["events"])
async def cancel_event(event_id: UUID, service: CalendarService = Depends(get_calendar_service)) -> EventResponse:
    return await service.cancel_event(str(event_id))


@router.get("/events/{event_id}/recipients", response_model=list[EventRecipientResponse], tags=["events"])
async def list_recipients(
    event_id: UUID,
    service: CalendarService = Depends(get_calendar_service),
) -> list[EventRecipientResponse]:
    return await service.list_recipients(str(event_id))


@router.post(
    "/events/{event_id}/recipients",
    response_model=EventRecipientResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["events"],
)
async def add_recipient(
    event_id: UUID,
    payload: EventRecipientCreate,
    service: CalendarService = Depends(get_calendar_service),
) -> EventRecipientResponse:
    return await service.add_recipient(str(event_id), payload)


@router.put("/events/{event_id}/recipients/{recipient_id}", response_model=EventRecipientResponse, tags=["events"])
async def update_recipient(
    event_id: UUID,
    recipient_id: UUID,
    payload: EventRecipientUpdate,
    service: CalendarService = Depends(get_calendar_service),
) -> EventRecipientResponse:
    return await service.update_recipient(str(event_id), str(recipient_id), payload)
