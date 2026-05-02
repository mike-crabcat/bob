"""Dashboard contact routes."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse

from cyborg_server.database import Database
from cyborg_server.dependencies import get_database

from ._helpers import _get_pending_approval_count, _render_template

router = APIRouter()


@router.get("/contacts", response_class=HTMLResponse)
async def contacts(
    request: Request,
    db: Database = Depends(get_database),
) -> Response:
    pending_count = await _get_pending_approval_count(db)

    contacts_data = await db.fetch_all(
        """
        SELECT id, name, phone_number, email, is_default, is_trusted,
               created_at, updated_at
        FROM contacts
        WHERE deleted_at IS NULL
        ORDER BY is_trusted DESC, name ASC
        """,
    )

    total = len(contacts_data)
    trusted_count = sum(1 for c in contacts_data if c.get("is_trusted"))
    untrusted_count = total - trusted_count

    contact_rows = [
        {
            "id": c["id"],
            "name": c["name"],
            "phone_number": c["phone_number"],
            "email": c.get("email"),
            "is_default": bool(c.get("is_default", 0)),
            "is_trusted": bool(c.get("is_trusted", 0)),
            "created_at": c["created_at"],
        }
        for c in contacts_data
    ]

    return _render_template("dashboard/contacts.html", request, {
        "pending_count": pending_count,
        "contacts": contact_rows,
        "total": total,
        "trusted_count": trusted_count,
        "untrusted_count": untrusted_count,
    })


@router.post("/contacts/{contact_id}/toggle-trust")
async def toggle_contact_trust(
    contact_id: str,
    db: Database = Depends(get_database),
) -> Response:
    existing = await db.fetch_one(
        "SELECT is_trusted FROM contacts WHERE id = ? AND deleted_at IS NULL",
        (contact_id,),
    )
    if not existing:
        return Response(content="Contact not found", status_code=404)

    new_trust = 0 if existing["is_trusted"] else 1
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "UPDATE contacts SET is_trusted = ?, updated_at = ? WHERE id = ?",
        (new_trust, now, contact_id),
    )

    return Response(
        status_code=303,
        headers={"Location": "/dashboard/contacts"},
    )
