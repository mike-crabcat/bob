"""Dashboard API: Contacts and contact entities."""

from __future__ import annotations

from fastapi import APIRouter

from server.routers.dashboard_api._common import *  # noqa: F403,F405
from server.repositories.contacts import ContactRepository


router = APIRouter()


@router.get("/api/contacts")
async def get_contacts(request: Request) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    contacts: list[dict[str, Any]] = []
    table_exists = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='contacts'"
    )
    if table_exists:
        rows = await ContactRepository(db).dashboard_list()
        for row in rows:
            contacts.append({
                "id": row["id"],
                "name": row["name"],
                "phone_number": row["phone_number"],
                "email": row["email"],
                "is_trusted": bool(row["is_trusted"]),
                "is_default": bool(row["is_default"]),
                "allow_inbound_dm": bool(row["allow_inbound_dm"]),
                "session_count": row["session_count"],
                "last_active": _utc(row["last_active"]),
                "created_at": _utc(row["created_at"]),
                "updated_at": _utc(row["updated_at"]),
            })
    return {"contacts": contacts}


@router.get("/api/contacts/{contact_id}")
async def get_contact_detail(request: Request, contact_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    contact = await ContactRepository(db).get(contact_id)
    if not contact:
        return {"id": None}

    sessions: list[dict[str, Any]] = []
    from server.repositories.participants import ParticipantRepository
    session_rows = await ParticipantRepository(db).contact_session_rollup(contact_id)
    for row in session_rows:
        sessions.append({
            "session_key": row["session_key"],
            "channel": _parse_channel(row["session_key"]),
            "call_count": row["call_count"],
            "last_active": _utc(row["last_active_at"]),
        })

    groups: list[dict[str, Any]] = []
    groups_table = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='whatsappgroup_members'"
    )
    if groups_table:
        from server.repositories.groups import GroupRepository
        group_rows = await GroupRepository(db).groups_for_contact(contact_id)
        for row in group_rows:
            groups.append({
                "name": row["name"],
                "jid": row["whatsapp_jid"],
                "is_admin": bool(row["is_admin"]),
                "joined_at": _utc(row["joined_at"]),
            })

    return {
        "id": contact["id"],
        "name": contact["name"],
        "phone_number": contact["phone_number"],
        "email": contact["email"],
        "is_trusted": bool(contact["is_trusted"]),
        "is_default": bool(contact["is_default"]),
        "allow_inbound_dm": bool(contact["allow_inbound_dm"]),
        "metadata": json.loads(contact["metadata"]) if contact["metadata"] else {},
        "sessions": sessions,
        "groups": groups,
        "created_at": _utc(contact["created_at"]),
        "updated_at": _utc(contact["updated_at"]),
    }


@router.put("/api/contacts/{contact_id}")
async def update_contact(request: Request, contact_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)

    body = await request.json()
    updates: dict[str, Any] = {}
    if "name" in body and body["name"] is not None:
        updates["name"] = str(body["name"]).strip()
    if "phone_number" in body and body["phone_number"] is not None:
        updates["phone_number"] = str(body["phone_number"])
    if "email" in body:
        updates["email"] = body["email"]
    if "is_trusted" in body and body["is_trusted"] is not None:
        updates["is_trusted"] = 1 if body["is_trusted"] else 0
    if "allow_inbound_dm" in body and body["allow_inbound_dm"] is not None:
        updates["allow_inbound_dm"] = 1 if body["allow_inbound_dm"] else 0

    if not updates:
        return {"ok": True, "updated": False}

    await ContactRepository(db).update_fields(contact_id, updates)

    # Propagate name change to linked person entity's display_name snapshot
    if "name" in updates:
        from server.context import AppContext
        from server.services.memory import MemoryService
        settings = request.app.state.settings
        ctx = AppContext(settings=settings, db=db)
        await MemoryService(ctx).sync_person_display_name_for_contact(
            contact_id, updates["name"],
        )

    return {"ok": True, "updated": True}


@router.get("/api/contacts/{contact_id}/entity")
async def get_contact_entity(request: Request, contact_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    row = await ContactRepository(db).get(contact_id)
    if not row:
        return {"error": "contact not found"}

    from server.context import AppContext
    from server.services.memory.service import MemoryService

    settings = request.app.state.settings
    ctx = AppContext(settings=settings, db=db)
    svc = MemoryService(ctx)

    # Find person entity: try contact_id claim first, then name-slug match
    entity_id: str | None = None
    hex8 = str(contact_id)[:8]
    from server.services.memory import admin as memory_admin
    claim_entity = await memory_admin.entity_id_for_contact_hex(db, hex8)
    if claim_entity:
        entity_id = claim_entity
    else:
        # Fallback: derive slug from contact name and look up person-{slug}
        import re
        name_row = await ContactRepository(db).get_any(contact_id)
        if name_row and name_row["name"]:
            slug = re.sub(r"[^a-z0-9\-]", "", name_row["name"].strip().lower().replace(" ", "-"))
            entity_id = f"person-{slug}"

    if not entity_id:
        return {"error": "not found"}

    entity = await svc.read_entity(settings.harness.workspace_dir, entity_id)
    if not entity:
        return {"error": "not found"}

    # Render entity claims
    from server.services.memory.claim_service import get_active_claims
    from server.services.memory.claim_types import render_entity

    claims = await get_active_claims(db, entity.entity_id)
    claim_dicts = [
        {"claim_type_key": c.claim_type_key, "object_id": c.object_id, "value": c.value}
        for c in claims
    ]
    rendered = await render_entity(entity.entity_type, entity.display_name, claim_dicts, entity_id=entity.entity_id, db=db)

    return {
        "entity_id": entity.entity_id,
        "entity_type": entity.entity_type,
        "display_name": entity.display_name,
        "status": entity.status,
        "rendered": rendered,
    }


@router.get("/api/contacts/{contact_id}/claims")
async def get_contact_claims(request: Request, contact_id: str) -> Any:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    row = await ContactRepository(db).get(contact_id)
    if not row:
        return {"error": "contact not found"}

    from server.services.memory.claim_service import get_active_claims

    # Find person entity: try contact_id claim first, then name-slug match
    entity_id: str | None = None
    hex8 = str(contact_id)[:8]
    from server.services.memory import admin as memory_admin
    claim_entity = await memory_admin.entity_id_for_contact_hex(db, hex8)
    if claim_entity:
        entity_id = claim_entity
    else:
        import re
        name_row = await ContactRepository(db).get_any(contact_id)
        if name_row and name_row["name"]:
            slug = re.sub(r"[^a-z0-9\-]", "", name_row["name"].strip().lower().replace(" ", "-"))
            entity_id = f"person-{slug}"

    if not entity_id:
        return []

    claims = await get_active_claims(db, entity_id)

    return [
        {
            "id": c.id,
            "claim_type_key": c.claim_type_key,
            "subject_id": c.subject_id,
            "object_id": c.object_id,
            "value": c.value,
            "status": c.status,
            "visibility": c.visibility,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in claims
    ]


