"""Shared service helpers."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any

from server.context import AppContext
from server.database import Database


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


def _parse_ts(value: str | datetime) -> "datetime | str":
    """Tolerant ISO 8601 parse (T or space separator, optional Z or
    +00:00 suffix). Returns the datetime, or the original value when it
    won't parse so callers can pass it through untouched."""
    if isinstance(value, datetime):
        return value
    candidate = value.strip()
    if not candidate:
        return value
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return value


def iso_utc(value: str | datetime | None = None) -> str:
    """Normalize a timestamp to canonical 'YYYY-MM-DDTHH:MM:SSZ' UTC.

    Accepts ISO 8601 variants (T or space separator, optional microseconds,
    optional +00:00 or Z suffix) and naive datetimes (assumed UTC). Strings
    that fail to parse are returned unchanged so we never silently corrupt data.
    """

    if value is None:
        return utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    dt = _parse_ts(value)
    if not isinstance(dt, datetime):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_iso(value: str | datetime | None = None) -> str:
    """Render a timestamp in server-local time with an explicit offset.

    LLM-facing tools must use this instead of raw DB timestamps: messages
    store UTC, and an unlabeled '2026-09-12 23:28:51' read as Perth wall
    clock is 8 h wrong and can cross the date boundary (2026-09-13: Bob
    narrated his own 07:28 Sunday advert post as '23:28 Saturday night'
    straight from a get_session_messages result). Same parse tolerance
    and naive-as-UTC assumption as iso_utc; unparseable strings pass
    through unchanged.
    """

    if value is None:
        return utcnow().astimezone().isoformat(sep=" ", timespec="seconds")
    dt = _parse_ts(value)
    if not isinstance(dt, datetime):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone().isoformat(sep=" ", timespec="seconds")


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
