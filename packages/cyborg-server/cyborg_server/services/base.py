"""Shared service helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from typing import Any

from cyborg_server.database import Database


def utcnow() -> datetime:
    """Return the current UTC timestamp."""

    return datetime.now(UTC)


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


def next_cron_occurrence(expression: str, start: datetime | None = None) -> datetime:
    """Compute the next timestamp matching a five-field cron rule."""

    reference = (start or utcnow()).astimezone(UTC).replace(second=0, microsecond=0) + timedelta(minutes=1)
    minute_values, hour_values, day_values, month_values, weekday_values = [
        _expand_cron_field(field, bounds)
        for field, bounds in zip(
            expression.split(),
            ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7)),
            strict=True,
        )
    ]

    for offset in range(0, 366 * 24 * 60):
        candidate = reference + timedelta(minutes=offset)
        weekday = (candidate.weekday() + 1) % 7
        if 7 in weekday_values:
            weekday_values = set(weekday_values)
            weekday_values.add(0)
        if (
            candidate.minute in minute_values
            and candidate.hour in hour_values
            and candidate.day in day_values
            and candidate.month in month_values
            and weekday in weekday_values
        ):
            return candidate

    raise ValueError("Unable to resolve the next cron occurrence within one year")


def _expand_cron_field(field: str, bounds: tuple[int, int]) -> set[int]:
    lower, upper = bounds
    values: set[int] = set()
    for token in field.split(","):
        values.update(_expand_cron_token(token, lower, upper))
    return values


def _expand_cron_token(token: str, lower: int, upper: int) -> set[int]:
    if token == "*":
        return set(range(lower, upper + 1))
    if token.startswith("*/"):
        step = int(token[2:])
        return set(range(lower, upper + 1, step))
    if "/" in token:
        base, step_text = token.split("/", 1)
        step = int(step_text)
        base_values = sorted(_expand_cron_token(base, lower, upper))
        return set(base_values[::step])
    if "-" in token:
        start_text, end_text = token.split("-", 1)
        start_value = int(start_text)
        end_value = int(end_text)
        return set(range(start_value, end_value + 1))
    return {int(token)}


class BaseService:
    """Base class for service helpers."""

    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def decode_json_fields(row: dict[str, Any] | None, *fields: str) -> dict[str, Any] | None:
        """Decode JSON fields on a row dictionary."""

        if row is None:
            return None
        for field in fields:
            default = None if field == "retry_config" else ([] if field.endswith("_ids") else {})
            row[field] = json_loads(row.get(field), default)
        return row
