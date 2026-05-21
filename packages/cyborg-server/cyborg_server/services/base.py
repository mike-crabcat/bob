"""Shared service helpers."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any

from cyborg_server.context import AppContext
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


class BaseService:
    """Base class for service helpers."""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.db: Database = ctx.db

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
