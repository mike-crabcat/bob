"""Business logic for calendars, events, and recipients."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from aiosqlite import Connection

from cyborg.database import Database
from cyborg.exceptions import NotFoundError
from cyborg.models import (
    CalendarCreate,
    CalendarResponse,
    CalendarUpdate,
    EventCreate,
    EventRecipientCreate,
    EventRecipientResponse,
    EventRecipientUpdate,
    EventResponse,
    EventStatus,
    EventUpdate,
)
from cyborg.services.base import BaseService, utcnow


class CalendarService(BaseService):
    """CRUD and lifecycle operations for calendars and events."""

    def __init__(self, db: Database) -> None:
        super().__init__(db)

    async def list_calendars(self) -> list[CalendarResponse]:
        rows = await self.db.fetch_all(
            "SELECT * FROM calendars WHERE deleted_at IS NULL ORDER BY is_default DESC, name ASC"
        )
        return [CalendarResponse.model_validate(row) for row in rows]

    async def get_calendar(self, calendar_id: str) -> CalendarResponse:
        row = await self._get_calendar_row(calendar_id)
        return CalendarResponse.model_validate(row)

    async def create_calendar(self, payload: CalendarCreate) -> CalendarResponse:
        calendar_id = str(uuid4())
        now = utcnow().isoformat()
        async with self.db.connection(write=True) as connection:
            if payload.is_default:
                await connection.execute("UPDATE calendars SET is_default = 0 WHERE deleted_at IS NULL")
            await connection.execute(
                """
                INSERT INTO calendars (id, name, description, color, is_default, created_at, deleted_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    calendar_id,
                    payload.name,
                    payload.description,
                    payload.color,
                    int(payload.is_default),
                    now,
                ),
            )
        return await self.get_calendar(calendar_id)

    async def update_calendar(self, calendar_id: str, payload: CalendarUpdate) -> CalendarResponse:
        await self._get_calendar_row(calendar_id)
        values = payload.model_dump(exclude_unset=True, mode="json")
        if not values:
            return await self.get_calendar(calendar_id)

        assignments = ", ".join(f"{field} = ?" for field in values)
        params = tuple(values.values()) + (calendar_id,)

        async with self.db.connection(write=True) as connection:
            if values.get("is_default") is True:
                await connection.execute("UPDATE calendars SET is_default = 0 WHERE deleted_at IS NULL")
            await connection.execute(f"UPDATE calendars SET {assignments} WHERE id = ? AND deleted_at IS NULL", params)
        return await self.get_calendar(calendar_id)

    async def delete_calendar(self, calendar_id: str) -> None:
        await self._get_calendar_row(calendar_id)
        await self.db.execute(
            "UPDATE calendars SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
            (utcnow().isoformat(), calendar_id),
        )

    async def list_events(
        self,
        *,
        calendar_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[EventResponse]:
        query = """
            SELECT e.*
            FROM events AS e
            INNER JOIN calendars AS c ON c.id = e.calendar_id
            WHERE e.deleted_at IS NULL AND c.deleted_at IS NULL
        """
        params: list[Any] = []
        if calendar_id is not None:
            query += " AND e.calendar_id = ?"
            params.append(calendar_id)
        if date_from is not None:
            query += " AND e.start_time >= ?"
            params.append(date_from)
        if date_to is not None:
            query += " AND e.start_time <= ?"
            params.append(date_to)
        query += " ORDER BY e.start_time ASC"
        rows = await self.db.fetch_all(query, tuple(params))
        return [EventResponse.model_validate(row) for row in rows]

    async def get_event(self, event_id: str) -> EventResponse:
        row = await self._get_event_row(event_id)
        return EventResponse.model_validate(row)

    async def create_event(self, payload: EventCreate) -> EventResponse:
        event_id = str(uuid4())
        now = utcnow().isoformat()
        async with self.db.connection(write=True) as connection:
            await self._ensure_calendar_exists(connection, str(payload.calendar_id))
            await connection.execute(
                """
                INSERT INTO events (
                    id, calendar_id, title, description, agenda, venue, start_time, end_time,
                    timezone, is_all_day, recurrence_rule, status, created_at, updated_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    event_id,
                    str(payload.calendar_id),
                    payload.title,
                    payload.description,
                    payload.agenda,
                    payload.venue,
                    payload.start_time.isoformat(),
                    payload.end_time.isoformat(),
                    payload.timezone,
                    int(payload.is_all_day),
                    payload.recurrence_rule,
                    payload.status.value,
                    now,
                    now,
                ),
            )
        return await self.get_event(event_id)

    async def update_event(self, event_id: str, payload: EventUpdate) -> EventResponse:
        existing = await self._get_event_row(event_id)
        values = payload.model_dump(exclude_unset=True, mode="json")
        if not values:
            return EventResponse.model_validate(existing)

        if "calendar_id" in values:
            async with self.db.connection(write=True) as connection:
                await self._ensure_calendar_exists(connection, values["calendar_id"])
        values["updated_at"] = utcnow().isoformat()
        assignments = ", ".join(f"{field} = ?" for field in values)
        await self.db.execute(
            f"UPDATE events SET {assignments} WHERE id = ? AND deleted_at IS NULL",
            tuple(values.values()) + (event_id,),
        )
        return await self.get_event(event_id)

    async def delete_event(self, event_id: str) -> None:
        await self._get_event_row(event_id)
        now = utcnow().isoformat()
        await self.db.execute(
            "UPDATE events SET deleted_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
            (now, now, event_id),
        )

    async def confirm_event(self, event_id: str) -> EventResponse:
        return await self._transition_event(event_id, EventStatus.CONFIRMED)

    async def cancel_event(self, event_id: str) -> EventResponse:
        return await self._transition_event(event_id, EventStatus.CANCELLED)

    async def list_recipients(self, event_id: str) -> list[EventRecipientResponse]:
        await self._get_event_row(event_id)
        rows = await self.db.fetch_all(
            "SELECT * FROM event_recipients WHERE event_id = ? ORDER BY name ASC, recipient_address ASC",
            (event_id,),
        )
        return [EventRecipientResponse.model_validate(row) for row in rows]

    async def add_recipient(self, event_id: str, payload: EventRecipientCreate) -> EventRecipientResponse:
        await self._get_event_row(event_id)
        recipient_id = str(uuid4())
        await self.db.execute(
            """
            INSERT INTO event_recipients (
                id, event_id, recipient_type, recipient_address, name, status, responded_at, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                recipient_id,
                event_id,
                payload.recipient_type.value,
                payload.recipient_address,
                payload.name,
                payload.status.value,
                payload.responded_at.isoformat() if payload.responded_at else None,
                payload.notes,
            ),
        )
        row = await self.db.fetch_one("SELECT * FROM event_recipients WHERE id = ?", (recipient_id,))
        return EventRecipientResponse.model_validate(row)

    async def update_recipient(
        self,
        event_id: str,
        recipient_id: str,
        payload: EventRecipientUpdate,
    ) -> EventRecipientResponse:
        await self._get_event_row(event_id)
        row = await self.db.fetch_one(
            "SELECT * FROM event_recipients WHERE id = ? AND event_id = ?",
            (recipient_id, event_id),
        )
        if row is None:
            raise NotFoundError(f"Recipient '{recipient_id}' was not found for event '{event_id}'")

        values = payload.model_dump(exclude_unset=True, mode="json")
        if not values:
            return EventRecipientResponse.model_validate(row)

        assignments = ", ".join(f"{field} = ?" for field in values)
        await self.db.execute(
            f"UPDATE event_recipients SET {assignments} WHERE id = ? AND event_id = ?",
            tuple(values.values()) + (recipient_id, event_id),
        )
        updated = await self.db.fetch_one("SELECT * FROM event_recipients WHERE id = ?", (recipient_id,))
        return EventRecipientResponse.model_validate(updated)

    async def _transition_event(self, event_id: str, status: EventStatus) -> EventResponse:
        await self._get_event_row(event_id)
        await self.db.execute(
            "UPDATE events SET status = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
            (status.value, utcnow().isoformat(), event_id),
        )
        return await self.get_event(event_id)

    async def _get_calendar_row(self, calendar_id: str) -> dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM calendars WHERE id = ? AND deleted_at IS NULL", (calendar_id,))
        if row is None:
            raise NotFoundError(f"Calendar '{calendar_id}' was not found")
        return row

    async def _get_event_row(self, event_id: str) -> dict[str, Any]:
        row = await self.db.fetch_one("SELECT * FROM events WHERE id = ? AND deleted_at IS NULL", (event_id,))
        if row is None:
            raise NotFoundError(f"Event '{event_id}' was not found")
        return row

    async def _ensure_calendar_exists(self, connection: Connection, calendar_id: str) -> None:
        cursor = await connection.execute(
            "SELECT id FROM calendars WHERE id = ? AND deleted_at IS NULL",
            (calendar_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise NotFoundError(f"Calendar '{calendar_id}' was not found")
