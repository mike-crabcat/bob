"""Dashboard API: Persona config and history."""

from __future__ import annotations

from fastapi import APIRouter

from bob_server.routers.dashboard_api._common import *  # noqa: F403,F405


router = APIRouter()


@router.get("/api/persona")
async def dashboard_get_persona(request: Request) -> dict[str, Any]:
    db = _db(request)
    row = await db.fetch_one("SELECT * FROM persona_records WHERE is_active = 1")
    if row is None:
        return {"data": None}
    return {"data": _persona_row_to_dict(row)}


@router.get("/api/persona/history")
async def dashboard_get_persona_history(request: Request) -> dict[str, Any]:
    db = _db(request)
    rows = await db.fetch_all("SELECT * FROM persona_records ORDER BY revision DESC")
    return {"data": [_persona_row_to_dict(r) for r in rows]}


@router.post("/api/persona")
async def dashboard_create_persona(request: Request) -> dict[str, Any]:
    import uuid
    from bob_server.models import PersonaUpdate
    db = _db(request)
    body = await request.json()
    payload = PersonaUpdate(**body)

    max_row = await db.fetch_one("SELECT MAX(revision) as max_rev FROM persona_records")
    next_revision = (max_row["max_rev"] or 0) + 1

    record_id = str(uuid.uuid4())
    config_json = json.dumps(payload.config.model_dump())

    await db.execute("UPDATE persona_records SET is_active = 0 WHERE is_active = 1")
    await db.execute(
        """INSERT INTO persona_records (id, revision, soul, identity, agents, user_content, config, is_active, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))""",
        (record_id, next_revision, payload.soul, payload.identity, payload.agents, payload.user_content, config_json),
    )

    row = await db.fetch_one("SELECT * FROM persona_records WHERE id = ?", (record_id,))
    return {"data": _persona_row_to_dict(row)}


@router.patch("/api/persona/{revision}/activate")
async def dashboard_activate_persona(request: Request, revision: int) -> dict[str, Any]:
    db = _db(request)
    row = await db.fetch_one("SELECT * FROM persona_records WHERE revision = ?", (revision,))
    if row is None:
        return {"error": f"Revision r{revision} not found"}

    await db.execute("UPDATE persona_records SET is_active = 0 WHERE is_active = 1")
    await db.execute("UPDATE persona_records SET is_active = 1 WHERE revision = ?", (revision,))

    row = await db.fetch_one("SELECT * FROM persona_records WHERE revision = ?", (revision,))
    return {"data": _persona_row_to_dict(row)}


def _persona_row_to_dict(row: Any) -> dict[str, Any]:
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
