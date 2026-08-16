"""Runtime dream configuration (dream_config table).

Env settings are boot defaults; values here override and are settable without
a restart via the /autoplan slash command, CLI, or dashboard.
"""

from __future__ import annotations

import json
from typing import Any

from bob_server.database import Database
from bob_server.services.base import iso_utc

RUNTIME_AUTO_APPROVE_PLANS = "auto_approve_plans"


async def get_runtime_value(db: Database, key: str, boot_default: Any) -> Any:
    """Read a runtime value, falling back to the env/boot default."""
    row = await db.fetch_one("SELECT value FROM dream_config WHERE key = ?", (key,))
    if row is None:
        return boot_default
    try:
        return json.loads(row["value"])
    except (ValueError, TypeError):
        return boot_default


async def set_runtime_value(db: Database, key: str, value: Any) -> None:
    await db.execute(
        "INSERT INTO dream_config (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, json.dumps(value), iso_utc()),
    )


async def get_auto_approve_plans(db: Database, boot_default: bool) -> bool:
    return bool(await get_runtime_value(db, RUNTIME_AUTO_APPROVE_PLANS, boot_default))


async def set_auto_approve_plans(db: Database, enabled: bool) -> None:
    await set_runtime_value(db, RUNTIME_AUTO_APPROVE_PLANS, bool(enabled))
