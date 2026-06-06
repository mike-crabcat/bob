"""HTTP routes for contact management."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from cyborg_server.database import Database
from cyborg_server.dependencies import get_database
from cyborg_server.models import ContactCreate, ContactResponse, ContactUpdate


router = APIRouter(prefix="/contacts", tags=["contacts"])


def _normalize_phone_number(phone: str) -> str:
    """Normalize phone number to +61 format."""
    # Remove all non-digit characters
    digits = re.sub(r"\D", "", phone)
    
    # If starts with 0, replace with +61
    if digits.startswith("0"):
        return "+61" + digits[1:]
    
    # If starts with 61 but no +, add +
    if digits.startswith("61") and not phone.strip().startswith("+"):
        return "+" + digits
    
    # If already has +, return as is (with just digits after +)
    if phone.strip().startswith("+"):
        return "+" + digits
    
    # Default: add +61 prefix
    return "+61" + digits


def _row_to_contact(row: dict[str, Any]) -> ContactResponse:
    """Convert a database row to a ContactResponse."""
    metadata = json.loads(row["metadata"]) if row["metadata"] else {}

    return ContactResponse(
        id=UUID(row["id"]),
        name=row["name"],
        phone_number=row["phone_number"],
        email=row["email"],
        metadata=metadata,
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        deleted_at=datetime.fromisoformat(row["deleted_at"]) if row["deleted_at"] else None,
        is_trusted=bool(row.get("is_trusted", 0)),
    )


@router.post("", response_model=ContactResponse, status_code=status.HTTP_201_CREATED)
async def create_contact(
    payload: ContactCreate,
    database: Database = Depends(get_database),
) -> ContactResponse:
    """Create a new contact."""
    contact_id = uuid4()
    now = datetime.now(timezone.utc).isoformat()
    normalized_phone = _normalize_phone_number(payload.phone_number) if payload.phone_number else None

    try:
        await database.execute(
            """
            INSERT INTO contacts (id, name, phone_number, email, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(contact_id),
                payload.name,
                normalized_phone,
                payload.email,
                json.dumps(payload.metadata),
                now,
                now,
            ),
        )
    except Exception as e:
        if "UNIQUE constraint failed" in str(e):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Contact with phone number {normalized_phone} already exists",
            )
        raise
    
    row = await database.fetch_one(
        "SELECT * FROM contacts WHERE id = ?",
        (str(contact_id),),
    )
    return _row_to_contact(row)


@router.get("", response_model=list[ContactResponse])
async def list_contacts(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    search: str | None = Query(None, min_length=1),
    database: Database = Depends(get_database),
) -> list[ContactResponse]:
    """List contacts with optional pagination and search."""
    base_query = "SELECT * FROM contacts WHERE deleted_at IS NULL"
    params: list[Any] = []
    
    if search:
        base_query += " AND (name LIKE ? OR phone_number LIKE ? OR email LIKE ?)"
        search_pattern = f"%{search}%"
        params.extend([search_pattern, search_pattern, search_pattern])
    
    base_query += " ORDER BY name LIMIT ? OFFSET ?"
    params.extend([limit, skip])
    
    rows = await database.fetch_all(base_query, tuple(params))
    return [_row_to_contact(row) for row in rows]


@router.get("/{contact_id}", response_model=ContactResponse)
async def get_contact(
    contact_id: UUID,
    database: Database = Depends(get_database),
) -> ContactResponse:
    """Get a single contact by ID."""
    row = await database.fetch_one(
        "SELECT * FROM contacts WHERE id = ? AND deleted_at IS NULL",
        (str(contact_id),),
    )
    
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Contact {contact_id} not found",
        )
    
    return _row_to_contact(row)


@router.put("/{contact_id}", response_model=ContactResponse)
async def update_contact(
    contact_id: UUID,
    payload: ContactUpdate,
    database: Database = Depends(get_database),
) -> ContactResponse:
    """Update a contact."""
    # Check if contact exists
    existing = await database.fetch_one(
        "SELECT * FROM contacts WHERE id = ? AND deleted_at IS NULL",
        (str(contact_id),),
    )
    
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Contact {contact_id} not found",
        )
    
    # Build update fields
    updates: dict[str, Any] = {"updated_at": datetime.now(timezone.utc).isoformat()}
    
    if payload.name is not None:
        updates["name"] = payload.name
    if payload.phone_number is not None:
        updates["phone_number"] = _normalize_phone_number(payload.phone_number)
    if payload.email is not None:
        updates["email"] = payload.email
    if payload.is_trusted is not None:
        updates["is_trusted"] = 1 if payload.is_trusted else 0
    if payload.metadata is not None:
        updates["metadata"] = json.dumps(payload.metadata)
    
    # Build SET clause
    set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
    values = list(updates.values()) + [str(contact_id)]
    
    try:
        await database.execute(
            f"UPDATE contacts SET {set_clause} WHERE id = ?",
            tuple(values),
        )
    except Exception as e:
        if "UNIQUE constraint failed" in str(e):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Contact with this phone number or email already exists",
            )
        raise
    
    row = await database.fetch_one(
        "SELECT * FROM contacts WHERE id = ?",
        (str(contact_id),),
    )
    return _row_to_contact(row)


@router.delete("/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_contact(
    contact_id: UUID,
    database: Database = Depends(get_database),
) -> Response:
    """Soft delete a contact."""
    # Check if contact exists
    existing = await database.fetch_one(
        "SELECT * FROM contacts WHERE id = ? AND deleted_at IS NULL",
        (str(contact_id),),
    )
    
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Contact {contact_id} not found",
        )
    
    # Soft delete
    now = datetime.now(timezone.utc).isoformat()
    await database.execute(
        "UPDATE contacts SET deleted_at = ? WHERE id = ?",
        (now, str(contact_id)),
    )
    
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/by-phone/{phone_number}", response_model=ContactResponse)
async def get_contact_by_phone(
    phone_number: str,
    database: Database = Depends(get_database),
) -> ContactResponse:
    """Lookup contact by phone number (normalized to +61 format)."""
    normalized = _normalize_phone_number(phone_number)
    
    row = await database.fetch_one(
        "SELECT * FROM contacts WHERE phone_number = ? AND deleted_at IS NULL",
        (normalized,),
    )
    
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Contact with phone number {normalized} not found",
        )
    
    return _row_to_contact(row)


@router.get("/by-email/{email}", response_model=ContactResponse)
async def get_contact_by_email(
    email: str,
    database: Database = Depends(get_database),
) -> ContactResponse:
    """Lookup contact by email address."""
    row = await database.fetch_one(
        "SELECT * FROM contacts WHERE email = ? AND deleted_at IS NULL",
        (email,),
    )
    
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Contact with email {email} not found",
        )
    
    return _row_to_contact(row)


@router.get("/by-whatsapp-group/{group_id}", response_model=list[ContactResponse])
async def get_contacts_by_whatsapp_group(
    group_id: str,
    database: Database = Depends(get_database),
) -> list[ContactResponse]:
    """Find all contacts that are members of a WhatsApp group."""
    rows = await database.fetch_all(
        """SELECT c.* FROM contacts c
           JOIN whatsappgroup_members gm ON gm.contact_id = c.id
           JOIN whatsappgroups g ON g.id = gm.group_id
           WHERE g.whatsapp_jid = ? AND c.deleted_at IS NULL AND gm.left_at IS NULL""",
        (group_id,),
    )

    return [_row_to_contact(row) for row in rows]


@router.get("/default", response_model=ContactResponse)
async def get_default_contact(
    database: Database = Depends(get_database),
) -> ContactResponse:
    """Get the current default contact for notifications."""
    row = await database.fetch_one(
        "SELECT * FROM contacts WHERE is_default = 1 AND deleted_at IS NULL LIMIT 1",
        (),
    )

    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No default contact configured",
        )

    return _row_to_contact(row)


@router.put("/{contact_id}/set-default", response_model=ContactResponse)
async def set_default_contact(
    contact_id: UUID,
    database: Database = Depends(get_database),
) -> ContactResponse:
    """Set a contact as the default for notifications."""
    # Check if contact exists
    existing = await database.fetch_one(
        "SELECT * FROM contacts WHERE id = ? AND deleted_at IS NULL",
        (str(contact_id),),
    )

    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Contact {contact_id} not found",
        )

    # Set as default (trigger will unset others)
    await database.execute(
        "UPDATE contacts SET is_default = 1 WHERE id = ?",
        (str(contact_id),),
    )

    row = await database.fetch_one(
        "SELECT * FROM contacts WHERE id = ?",
        (str(contact_id),),
    )
    return _row_to_contact(row)


@router.delete("/default", status_code=status.HTTP_204_NO_CONTENT)
async def clear_default_contact(
    database: Database = Depends(get_database),
) -> Response:
    """Clear the default contact."""
    await database.execute(
        "UPDATE contacts SET is_default = 0 WHERE is_default = 1",
        (),
    )

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{contact_id}/entity")
async def get_contact_entity(
    contact_id: UUID,
    request: Request,
    database: Database = Depends(get_database),
) -> dict[str, Any]:
    """Get the entity document for a contact from the memory system."""
    row = await database.fetch_one(
        "SELECT id FROM contacts WHERE id = ? AND deleted_at IS NULL",
        (str(contact_id),),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Contact not found")

    from cyborg_server.context import AppContext
    from cyborg_server.services.memory.entity_resolver import canonical_contact_id
    from cyborg_server.services.memory.service import MemoryService

    settings = request.app.state.settings
    ctx = AppContext(settings=settings, db=database)
    entity_id = canonical_contact_id(str(contact_id))
    svc = MemoryService(ctx)
    entity = svc.read_entity(settings.harness.workspace_dir, entity_id)

    if not entity:
        raise HTTPException(status_code=404, detail="No entity document found for this contact")

    return {
        "entity_id": entity.entity_id,
        "entity_type": entity.entity_type,
        "display_name": entity.display_name,
        "status": entity.status,
    }


@router.get("/{contact_id}/claims")
async def get_contact_claims(
    contact_id: UUID,
    request: Request,
    database: Database = Depends(get_database),
) -> list[dict[str, Any]]:
    """Get active claims for a contact from the memory system."""
    row = await database.fetch_one(
        "SELECT id FROM contacts WHERE id = ? AND deleted_at IS NULL",
        (str(contact_id),),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Contact not found")

    from cyborg_server.services.memory.claim_service import get_active_claims
    from cyborg_server.services.memory.entity_resolver import canonical_contact_id

    entity_id = canonical_contact_id(str(contact_id))
    claims = await get_active_claims(database, entity_id)

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
