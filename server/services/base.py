"""Shared service helpers."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any

from bob_server.context import AppContext
from bob_server.database import Database


def utcnow() -> datetime:
    """Return the current UTC timestamp.

    Bob Events §4.3: an override seam so tests can time-travel unit-level
    wakeup/deadline logic (full event-loop fake-clock control stays out of
    scope; the e2e rehearsal uses compressed deadlines instead). Callers
    import the function object, and the override is read at call time.
    """

    return _CLOCK_OVERRIDE if _CLOCK_OVERRIDE is not None else datetime.now(UTC)


_CLOCK_OVERRIDE: datetime | None = None


def set_clock_override(value: datetime | None) -> None:
    global _CLOCK_OVERRIDE
    _CLOCK_OVERRIDE = value


def clear_clock_override() -> None:
    set_clock_override(None)


def iso_utc(value: str | datetime | None = None) -> str:
    """Normalize a timestamp to canonical 'YYYY-MM-DDTHH:MM:SSZ' UTC.

    Accepts ISO 8601 variants (T or space separator, optional microseconds,
    optional +00:00 or Z suffix) and naive datetimes (assumed UTC). Strings
    that fail to parse are returned unchanged so we never silently corrupt data.
    """

    if value is None:
        return utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, datetime):
        dt = value
    else:
        candidate = value.strip()
        if not candidate:
            return value
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def json_dumps(value: Any) -> str | None:
    """Encode a JSON-compatible value for SQLite storage."""

    if value is None:
        return None
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def json_loads(value: str | None, default: Any) -> Any:
    """Decode JSON from SQLite storage."""

    if not value:
        return default
    return json.loads(value)


class BaseService:
    """Base class for service helpers."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.db: Database = ctx.db

    @classmethod
    def from_db(cls, db: "Database"):
        """Construct for callers that only hold a db handle (read paths)."""
        svc = cls.__new__(cls)
        svc.ctx = None
        svc.db = db
        return svc

    def _get_settings(self) -> "Settings":
        """Return the application settings."""
        return self.ctx.settings

    @staticmethod
    def decode_json_fields(row: dict[str, Any] | None, *fields: str) -> dict[str, Any] | None:
        """Decode JSON fields on a row dictionary."""

        if row is None:
            return None
        for field in fields:
            default = None if field == "retry_config" else ([] if field.endswith("_ids") else {})
            row[field] = json_loads(row.get(field), default)
        return row
