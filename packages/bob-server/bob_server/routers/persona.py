"""API router for persona management."""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from bob_server.dependencies import get_app_context
from bob_server.context import AppContext
from bob_server.models import PersonaUpdate


router = APIRouter(tags=["persona"])


def _row_to_dict(row: Any) -> dict[str, Any]:
    config = json.loads(row["config"]) if isinstance(row["config"], str) else row["config"]
    return {
        "id": row["id"],
        "revision": row["revision"],
        "soul": row["soul"],
        "identity": row["identity"],
        "agents": row["agents"],
        "user_content": row["user_content"],
        "config": config,
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
    }


@router.get("/persona")
async def get_active_persona(
    ctx: AppContext = Depends(get_app_context),
) -> dict[str, Any]:
    """Return the currently active persona record."""
    row = await ctx.db.fetch_one("SELECT * FROM persona_records WHERE is_active = 1")
    if row is None:
        raise HTTPException(status_code=404, detail="No active persona record")
    return {"data": _row_to_dict(row)}


@router.get("/persona/history")
async def get_persona_history(
    ctx: AppContext = Depends(get_app_context),
) -> dict[str, Any]:
    """Return all persona records ordered by revision descending."""
    rows = await ctx.db.fetch_all(
        "SELECT * FROM persona_records ORDER BY revision DESC"
    )
    return {"data": [_row_to_dict(r) for r in rows]}


@router.post("/persona", status_code=status.HTTP_201_CREATED)
async def create_persona(
    payload: PersonaUpdate,
    ctx: AppContext = Depends(get_app_context),
) -> dict[str, Any]:
    """Create a new persona record and activate it."""
    # Get next revision number
    max_row = await ctx.db.fetch_one(
        "SELECT MAX(revision) as max_rev FROM persona_records"
    )
    next_revision = (max_row["max_rev"] or 0) + 1

    record_id = str(uuid.uuid4())
    config_json = json.dumps(payload.config.model_dump())

    # Deactivate current active record
    await ctx.db.execute("UPDATE persona_records SET is_active = 0 WHERE is_active = 1")

    # Insert new record as active
    await ctx.db.execute(
        """INSERT INTO persona_records (id, revision, soul, identity, agents, user_content, config, is_active, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))""",
        (record_id, next_revision, payload.soul, payload.identity, payload.agents, payload.user_content, config_json),
    )

    row = await ctx.db.fetch_one("SELECT * FROM persona_records WHERE id = ?", (record_id,))
    return {"data": _row_to_dict(row)}


@router.patch("/persona/{revision}/activate")
async def activate_persona(
    revision: int,
    ctx: AppContext = Depends(get_app_context),
) -> dict[str, Any]:
    """Activate a specific persona revision."""
    row = await ctx.db.fetch_one(
        "SELECT * FROM persona_records WHERE revision = ?", (revision,)
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Revision r{revision} not found")

    # Swap active flag
    await ctx.db.execute("UPDATE persona_records SET is_active = 0 WHERE is_active = 1")
    await ctx.db.execute(
        "UPDATE persona_records SET is_active = 1 WHERE revision = ?", (revision,)
    )

    row = await ctx.db.fetch_one("SELECT * FROM persona_records WHERE revision = ?", (revision,))
    return {"data": _row_to_dict(row)}
