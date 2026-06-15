"""Dashboard API: Contacts and contact entities."""

from __future__ import annotations

from fastapi import APIRouter

from bob_server.routers.dashboard_api._common import *  # noqa: F403,F405


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
        rows = await db.fetch_all(
            """SELECT c.id, c.name, c.phone_number, c.email,
                      c.is_trusted, c.is_default,
                      c.created_at, c.updated_at,
                      (SELECT COUNT(*) FROM session_participants sp WHERE sp.contact_id = c.id) as session_count,
                      (SELECT MAX(sp.last_active_at) FROM session_participants sp WHERE sp.contact_id = c.id) as last_active
               FROM contacts c
               WHERE c.deleted_at IS NULL
               ORDER BY c.name"""
        )
        for row in rows:
            contacts.append({
                "id": row["id"],
                "name": row["name"],
                "phone_number": row["phone_number"],
                "email": row["email"],
                "is_trusted": bool(row["is_trusted"]),
                "is_default": bool(row["is_default"]),
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
    contact = await db.fetch_one(
        """SELECT id, name, phone_number, email, metadata,
                  is_trusted, is_default, created_at, updated_at
           FROM contacts WHERE id = ? AND deleted_at IS NULL""",
        (contact_id,),
    )
    if not contact:
        return {"id": None}

    sessions: list[dict[str, Any]] = []
    participants_table = await db.fetch_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_participants'"
    )
    if participants_table:
        session_rows = await db.fetch_all(
            """SELECT sp.session_key, sp.last_active_at,
                      (SELECT COUNT(*) FROM llm_call_log l WHERE l.session_key = sp.session_key) as call_count
               FROM session_participants sp
               WHERE sp.contact_id = ?
               ORDER BY sp.last_active_at DESC""",
            (contact_id,),
        )
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
        group_rows = await db.fetch_all(
            """SELECT g.name, g.whatsapp_jid, gm.is_admin, gm.joined_at
               FROM whatsappgroup_members gm
               JOIN whatsappgroups g ON g.id = gm.group_id
               WHERE gm.contact_id = ? AND gm.left_at IS NULL AND g.deleted_at IS NULL
               ORDER BY g.name""",
            (contact_id,),
        )
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

    if not updates:
        return {"ok": True, "updated": False}

    updates["updated_at"] = _utc_now()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [contact_id]
    await db.execute(
        f"UPDATE contacts SET {set_clause} WHERE id = ? AND deleted_at IS NULL",
        tuple(values),
    )
    return {"ok": True, "updated": True}


@router.get("/api/contacts/{contact_id}/entity")
async def get_contact_entity(request: Request, contact_id: str) -> dict[str, Any]:
    if not _check_auth(request):
        return {"error": "unauthorized"}
    db = _db(request)
    row = await db.fetch_one("SELECT id FROM contacts WHERE id = ? AND deleted_at IS NULL", (contact_id,))
    if not row:
        return {"error": "contact not found"}

    from bob_server.context import AppContext
    from bob_server.services.memory.service import MemoryService

    settings = request.app.state.settings
    ctx = AppContext(settings=settings, db=db)
    svc = MemoryService(ctx)

    # Find person entity: try contact_id claim first, then name-slug match
    entity_id: str | None = None
    hex8 = str(contact_id)[:8]
    claim_row = await db.fetch_one(
        "SELECT subject_id FROM memory_claims "
        "WHERE claim_type_key = 'contact_id' AND value = ? AND status = 'active' LIMIT 1",
        (hex8,),
    )
    if claim_row:
        entity_id = claim_row["subject_id"]
    else:
        # Fallback: derive slug from contact name and look up person-{slug}
        import re
        name_row = await db.fetch_one("SELECT name FROM contacts WHERE id = ?", (contact_id,))
        if name_row and name_row["name"]:
            slug = re.sub(r"[^a-z0-9\-]", "", name_row["name"].strip().lower().replace(" ", "-"))
            entity_id = f"person-{slug}"

    if not entity_id:
        return {"error": "not found"}

    entity = await svc.read_entity(settings.harness.workspace_dir, entity_id)
    if not entity:
        return {"error": "not found"}

    # Render entity claims
    from bob_server.services.memory.claim_service import get_active_claims
    from bob_server.services.memory.claim_types import render_entity

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
    row = await db.fetch_one("SELECT id FROM contacts WHERE id = ? AND deleted_at IS NULL", (contact_id,))
    if not row:
        return {"error": "contact not found"}

    from bob_server.services.memory.claim_service import get_active_claims

    # Find person entity: try contact_id claim first, then name-slug match
    entity_id: str | None = None
    hex8 = str(contact_id)[:8]
    claim_row = await db.fetch_one(
        "SELECT subject_id FROM memory_claims "
        "WHERE claim_type_key = 'contact_id' AND value = ? AND status = 'active' LIMIT 1",
        (hex8,),
    )
    if claim_row:
        entity_id = claim_row["subject_id"]
    else:
        import re
        name_row = await db.fetch_one("SELECT name FROM contacts WHERE id = ?", (contact_id,))
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
            "source_bulletins": c.source_bulletins,
            "visibility": c.visibility,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in claims
    ]


