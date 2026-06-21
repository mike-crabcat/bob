"""DB CRUD for routines."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from bob_server.services.base import BaseService

_ROUTINE_COLUMNS = (
    "id, session_key, name, schedule, prompt, enabled, next_run_at, last_run_at, "
    "timezone, valid_from, valid_until, created_at, updated_at"
)


def _routine_tz(row: dict[str, Any]) -> ZoneInfo:
    """Resolve a routine's timezone, falling back to server local."""
    name = row.get("timezone")
    if name:
        return ZoneInfo(name)
    return datetime.now().astimezone().tzinfo  # type: ignore[return-value]


def _parse_bound(value: str, tz: ZoneInfo) -> datetime | None:
    """Parse a validity bound into a tz-aware datetime.

    Naive datetimes (including date-only strings) are localized to `tz`. Malformed
    input returns None so callers can treat the bound as open rather than dropping
    the routine silently.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def _outside_validity_window(row: dict[str, Any]) -> bool:
    """True if 'now' in the routine's tz falls outside [valid_from, valid_until].

    Bounds are inclusive. Date-only upper bounds extend to end-of-day in the
    routine's tz so the routine still fires on that date.
    """
    tz = _routine_tz(row)
    now_local = datetime.now(tz)

    valid_from = row.get("valid_from")
    if valid_from:
        bound = _parse_bound(valid_from, tz)
        if bound is not None and now_local < bound:
            return True

    valid_until = row.get("valid_until")
    if valid_until:
        bound = _parse_bound(valid_until, tz)
        if bound is not None:
            if "T" not in valid_until:
                bound = bound + timedelta(days=1)
            if now_local >= bound:
                return True

    return False


class RoutineService(BaseService):
    async def list_routines(self, session_key: str) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            f"SELECT {_ROUTINE_COLUMNS} FROM routines WHERE session_key = ? ORDER BY name",
            (session_key,),
        )
        return [dict(r) for r in rows] if rows else []

    async def get_routine(self, session_key: str, name: str) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            f"SELECT {_ROUTINE_COLUMNS} FROM routines WHERE session_key = ? AND name = ?",
            (session_key, name),
        )
        return dict(row) if row else None

    async def upsert_routine(
        self,
        session_key: str,
        name: str,
        schedule: str,
        prompt: str,
        enabled: bool = True,
        next_run_at: str | None = None,
        *,
        timezone: str | None = None,
        valid_from: str | None = None,
        valid_until: str | None = None,
    ) -> dict[str, Any]:
        existing = await self.get_routine(session_key, name)
        now = datetime.now().astimezone().isoformat()

        if existing:
            await self.db.execute(
                "UPDATE routines SET schedule = ?, prompt = ?, enabled = ?, next_run_at = ?, "
                "timezone = ?, valid_from = ?, valid_until = ?, updated_at = ? "
                "WHERE session_key = ? AND name = ?",
                (schedule, prompt, int(enabled), next_run_at, timezone, valid_from, valid_until, now, session_key, name),
            )
        else:
            await self.db.execute(
                "INSERT INTO routines (id, session_key, name, schedule, prompt, enabled, next_run_at, "
                "timezone, valid_from, valid_until, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), session_key, name, schedule, prompt, int(enabled), next_run_at,
                 timezone, valid_from, valid_until, now, now),
            )

        result = await self.get_routine(session_key, name)
        assert result is not None
        return result

    async def delete_routine(self, session_key: str, name: str) -> bool:
        count = await self.db.execute(
            "DELETE FROM routines WHERE session_key = ? AND name = ?",
            (session_key, name),
        )
        return count > 0

    async def get_due_routines(self) -> list[dict[str, Any]]:
        now = datetime.now().astimezone().isoformat()
        rows = await self.db.fetch_all(
            f"SELECT {_ROUTINE_COLUMNS} FROM routines WHERE enabled = 1 AND next_run_at <= ?",
            (now,),
        )
        due: list[dict[str, Any]] = []
        for r in rows or []:
            row = dict(r)
            if _outside_validity_window(row):
                continue
            due.append(row)
        return due

    async def claim(self, routine_id: str, next_run_at: str) -> bool:
        """Atomically advance next_run_at. Returns True if this caller won the claim.

        Guards against duplicate dispatch when the heartbeat ticks faster than
        the routine body runs: a second heartbeat's UPDATE matches zero rows
        because next_run_at has already moved past `now`.
        """
        now = datetime.now().astimezone().isoformat()
        count = await self.db.execute(
            "UPDATE routines SET next_run_at = ?, updated_at = ? "
            "WHERE id = ? AND next_run_at <= ?",
            (next_run_at, now, routine_id, now),
        )
        return count > 0

    async def mark_run(self, routine_id: str) -> None:
        """Record last_run_at on a routine already claimed via claim()."""
        now = datetime.now().astimezone().isoformat()
        await self.db.execute(
            "UPDATE routines SET last_run_at = ?, updated_at = ? WHERE id = ?",
            (now, now, routine_id),
        )
