"""Dashboard API: Persona config and history."""

from __future__ import annotations

from fastapi import APIRouter

from bob_server.routers.dashboard_api._common import *  # noqa: F403,F405
from bob_server.services import persona as persona_service


router = APIRouter()


@router.get("/api/persona")
async def dashboard_get_persona(request: Request) -> dict[str, Any]:
    db = _db(request)
    row = await persona_service.active_record(db)
    if row is None:
        return {"data": None}
    return {"data": _persona_row_to_dict(row)}


@router.get("/api/persona/history")
async def dashboard_get_persona_history(request: Request) -> dict[str, Any]:
    db = _db(request)
    rows = await persona_service.list_records(db)
    return {"data": [_persona_row_to_dict(r) for r in rows]}


@router.post("/api/persona")
async def dashboard_create_persona(request: Request) -> dict[str, Any]:
    import uuid
    from bob_server.models import PersonaUpdate
    db = _db(request)
    body = await request.json()
    payload = PersonaUpdate(**body)

    next_revision = (await persona_service.max_revision(db)) + 1

    record_id = str(uuid.uuid4())
    config_json = json.dumps(payload.config.model_dump())

    await persona_service.insert_active_record(
        db, record_id=record_id, revision=next_revision,
        soul=payload.soul, identity=payload.identity, agents=payload.agents,
        user_content=payload.user_content, config_json=config_json)

    row = await persona_service.record_by_id(db, record_id)
    return {"data": _persona_row_to_dict(row)}


@router.patch("/api/persona/{revision}/activate")
async def dashboard_activate_persona(request: Request, revision: int) -> dict[str, Any]:
    db = _db(request)
    row = await persona_service.record_by_revision(db, revision)
    if row is None:
        return {"error": f"Revision r{revision} not found"}

    await persona_service.activate_revision(db, revision)
    row = await persona_service.record_by_revision(db, revision)
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
