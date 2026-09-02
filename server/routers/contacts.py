"""HTTP routes for contact management."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from server.database import Database
from server.dependencies import get_database
from server.models import ContactCreate, ContactResponse, ContactUpdate
from server.repositories.contacts import ContactRepository
from server.services.phone_utils import normalize_phone


router = APIRouter(prefix="/contacts", tags=["contacts"])


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
        allow_inbound_dm=bool(row.get("allow_inbound_dm", 1)),
    )


@router.post("", response_model=ContactResponse, status_code=status.HTTP_201_CREATED)
async def create_contact(
    payload: ContactCreate,
    database: Database = Depends(get_database),
) -> ContactResponse:
    """Create a new contact."""
    contact_id = uuid4()
    now = datetime.now(timezone.utc).isoformat()
    normalized_phone = normalize_phone(payload.phone_number) if payload.phone_number else None
    if payload.phone_number and normalized_phone is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Could not parse phone number: {payload.phone_number}",
        )

    try:
        await ContactRepository(database).create_full(
            contact_id=str(contact_id),
            name=payload.name,
            phone_number=normalized_phone,
            email=payload.email,
            metadata_json=json.dumps(payload.metadata),
            allow_inbound_dm=1 if payload.allow_inbound_dm else 0,
        )
    except Exception as e:
        if "UNIQUE constraint failed" in str(e):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Contact with phone number {normalized_phone} already exists",
            )
        raise
    
    row = await ContactRepository(database).get_any(str(contact_id))
    return _row_to_contact(row)


@router.get("", response_model=list[ContactResponse])
async def list_contacts(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    search: str | None = Query(None, min_length=1),
    database: Database = Depends(get_database),
) -> list[ContactResponse]:
    """List contacts with optional pagination and search."""
    rows = await ContactRepository(database).list_paged(
        skip=skip, limit=limit, search=search)
    return [_row_to_contact(row) for row in rows]


@router.get("/{contact_id}", response_model=ContactResponse)
async def get_contact(
    contact_id: UUID,
    database: Database = Depends(get_database),
) -> ContactResponse:
    """Get a single contact by ID."""
    row = await ContactRepository(database).get(str(contact_id))
    
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
    request: Request,
    database: Database = Depends(get_database),
) -> ContactResponse:
    """Update a contact."""
    # Check if contact exists
    existing = await ContactRepository(database).get(str(contact_id))

    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Contact {contact_id} not found",
        )

    # Build update fields
    updates: dict[str, Any] = {}

    if payload.name is not None:
        updates["name"] = payload.name
    if payload.phone_number is not None:
        normalized = normalize_phone(payload.phone_number)
        if normalized is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Could not parse phone number: {payload.phone_number}",
            )
        updates["phone_number"] = normalized
    if payload.email is not None:
        updates["email"] = payload.email
    if payload.is_trusted is not None:
        updates["is_trusted"] = 1 if payload.is_trusted else 0
    if payload.allow_inbound_dm is not None:
        updates["allow_inbound_dm"] = 1 if payload.allow_inbound_dm else 0
    if payload.metadata is not None:
        updates["metadata"] = json.dumps(payload.metadata)

    try:
        await ContactRepository(database).update_fields(str(contact_id), updates)
    except Exception as e:
        if "UNIQUE constraint failed" in str(e):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Contact with this phone number or email already exists",
            )
        raise

    # Propagate name change to linked person entity's display_name snapshot
    if payload.name is not None:
        from server.context import AppContext
        from server.services.memory import MemoryService
        ctx = AppContext(settings=request.app.state.settings, db=database)
        await MemoryService(ctx).sync_person_display_name_for_contact(
            str(contact_id), payload.name,
        )

    row = await ContactRepository(database).get_any(str(contact_id))
    return _row_to_contact(row)


@router.delete("/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_contact(
    contact_id: UUID,
    request: Request,
    database: Database = Depends(get_database),
) -> Response:
    """Soft delete a contact."""
    # Check if contact exists
    existing = await ContactRepository(database).get(str(contact_id))

    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Contact {contact_id} not found",
        )

    # Soft delete
    await ContactRepository(database).soft_delete(str(contact_id))

    # Retire the contact_id claim so the link doesn't dangle
    from server.context import AppContext
    from server.services.memory import MemoryService
    ctx = AppContext(settings=request.app.state.settings, db=database)
    await MemoryService(ctx).retire_contact_id_claim(str(contact_id))

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/by-phone/{phone_number}", response_model=ContactResponse)
async def get_contact_by_phone(
    phone_number: str,
    database: Database = Depends(get_database),
) -> ContactResponse:
    """Lookup contact by phone number (normalized to +61 format)."""
    normalized = normalize_phone(phone_number)
    if normalized is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Could not parse phone number: {phone_number}",
        )
    
    row = await ContactRepository(database).get_by_phone(normalized)
    
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
    row = await ContactRepository(database).get_by_email(email)
    
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
    rows = await ContactRepository(database).members_of_group_jid(group_id)

    return [_row_to_contact(row) for row in rows]


@router.get("/default", response_model=ContactResponse)
async def get_default_contact(
    database: Database = Depends(get_database),
) -> ContactResponse:
    """Get the current default contact for notifications."""
    row = await ContactRepository(database).get_default()

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
    existing = await ContactRepository(database).get(str(contact_id))

    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Contact {contact_id} not found",
        )

    # Set as default (trigger will unset others)
    await ContactRepository(database).set_default(str(contact_id))

    row = await ContactRepository(database).get_any(str(contact_id))
    return _row_to_contact(row)


@router.delete("/default", status_code=status.HTTP_204_NO_CONTENT)
async def clear_default_contact(
    database: Database = Depends(get_database),
) -> Response:
    """Clear the default contact."""
    await ContactRepository(database).clear_default()

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{contact_id}/entity")
async def get_contact_entity(
    contact_id: UUID,
    request: Request,
    database: Database = Depends(get_database),
) -> dict[str, Any]:
    """Get the entity document for a contact from the memory system."""
    row = await ContactRepository(database).get(str(contact_id))
    if not row:
        raise HTTPException(status_code=404, detail="Contact not found")

    from server.context import AppContext
    from server.services.memory.entity_resolver import canonical_contact_id
    from server.services.memory.service import MemoryService

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
    row = await ContactRepository(database).get(str(contact_id))
    if not row:
        raise HTTPException(status_code=404, detail="Contact not found")

    from server.services.memory.claim_service import get_active_claims
    from server.services.memory.entity_resolver import canonical_contact_id

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
            "visibility": c.visibility,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in claims
    ]
