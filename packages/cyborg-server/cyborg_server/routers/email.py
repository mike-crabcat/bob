"""HTTP routes for email relay via AgentMail."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from cyborg_server.database import Database
from cyborg_server.dependencies import get_database
from cyborg_server.models import (
    EmailInboxCreate,
    EmailInboxResponse,
    EmailInboxUpdate,
    EmailReplyRequest,
    EmailSendRequest,
    EmailThreadResponse,
)
from cyborg_server.services.base import json_dumps, json_loads, utcnow


router = APIRouter(prefix="/api/v1/email", tags=["email"])


def _row_to_inbox(row: dict[str, Any]) -> EmailInboxResponse:
    return EmailInboxResponse(
        id=UUID(row["id"]),
        agentmail_inbox_id=row["agentmail_inbox_id"],
        display_name=row["display_name"],
        email_address=row["email_address"],
        is_active=bool(row.get("is_active", 1)),
        last_polled_at=datetime.fromisoformat(row["last_polled_at"]) if row.get("last_polled_at") else None,
        metadata=json_loads(row.get("metadata"), {}),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _row_to_thread(row: dict[str, Any]) -> EmailThreadResponse:
    return EmailThreadResponse(
        id=UUID(row["id"]),
        inbox_id=UUID(row["inbox_id"]),
        agentmail_thread_id=row["agentmail_thread_id"],
        subject=row.get("subject"),
        contact_id=UUID(row["contact_id"]) if row.get("contact_id") else None,
        project_id=UUID(row["project_id"]) if row.get("project_id") else None,
        session_key=row["session_key"],
        message_count=int(row.get("message_count") or 0),
        last_message_at=datetime.fromisoformat(row["last_message_at"]) if row.get("last_message_at") else None,
        is_active=bool(row.get("is_active", 1)),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


# ---------------------------------------------------------------------------
# Inboxes
# ---------------------------------------------------------------------------


@router.post("/inboxes", response_model=EmailInboxResponse, status_code=status.HTTP_201_CREATED)
async def register_inbox(
    payload: EmailInboxCreate,
    database: Database = Depends(get_database),
) -> EmailInboxResponse:
    """Register an AgentMail inbox for email relay."""
    inbox_id = str(uuid4())
    now = utcnow().isoformat()
    await database.execute(
        """
        INSERT INTO email_inboxes (id, agentmail_inbox_id, display_name, email_address, metadata, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            inbox_id,
            payload.agentmail_inbox_id,
            payload.display_name,
            payload.email_address,
            json_dumps(payload.metadata),
            now,
            now,
        ),
    )
    row = await database.fetch_one("SELECT * FROM email_inboxes WHERE id = ?", (inbox_id,))
    return _row_to_inbox(row)


@router.get("/inboxes", response_model=list[EmailInboxResponse])
async def list_inboxes(
    active_only: bool = Query(default=True),
    database: Database = Depends(get_database),
) -> list[EmailInboxResponse]:
    """List registered email inboxes."""
    query = "SELECT * FROM email_inboxes WHERE deleted_at IS NULL"
    if active_only:
        query += " AND is_active = 1"
    query += " ORDER BY created_at ASC"
    rows = await database.fetch_all(query)
    return [_row_to_inbox(row) for row in rows]


@router.get("/inboxes/{inbox_id}", response_model=EmailInboxResponse)
async def get_inbox(
    inbox_id: UUID,
    database: Database = Depends(get_database),
) -> EmailInboxResponse:
    """Get a registered email inbox."""
    row = await database.fetch_one(
        "SELECT * FROM email_inboxes WHERE id = ? AND deleted_at IS NULL",
        (str(inbox_id),),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Inbox not found")
    return _row_to_inbox(row)


@router.patch("/inboxes/{inbox_id}", response_model=EmailInboxResponse)
async def update_inbox(
    inbox_id: UUID,
    payload: EmailInboxUpdate,
    database: Database = Depends(get_database),
) -> EmailInboxResponse:
    """Update a registered email inbox."""
    existing = await database.fetch_one(
        "SELECT * FROM email_inboxes WHERE id = ? AND deleted_at IS NULL",
        (str(inbox_id),),
    )
    if existing is None:
        raise HTTPException(status_code=404, detail="Inbox not found")

    values = payload.model_dump(exclude_unset=True, mode="json")
    if not values:
        return _row_to_inbox(existing)

    if "metadata" in values and values["metadata"] is not None:
        values["metadata"] = json_dumps(values["metadata"])
    if "is_active" in values:
        values["is_active"] = 1 if values["is_active"] else 0
    values["updated_at"] = utcnow().isoformat()

    assignments = ", ".join(f"{field} = ?" for field in values)
    await database.execute(
        f"UPDATE email_inboxes SET {assignments} WHERE id = ?",
        tuple(values.values()) + (str(inbox_id),),
    )
    row = await database.fetch_one("SELECT * FROM email_inboxes WHERE id = ?", (str(inbox_id),))
    return _row_to_inbox(row)


@router.delete("/inboxes/{inbox_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_inbox(
    inbox_id: UUID,
    database: Database = Depends(get_database),
) -> None:
    """Soft-delete a registered email inbox."""
    existing = await database.fetch_one(
        "SELECT * FROM email_inboxes WHERE id = ? AND deleted_at IS NULL",
        (str(inbox_id),),
    )
    if existing is None:
        raise HTTPException(status_code=404, detail="Inbox not found")
    now = utcnow().isoformat()
    await database.execute(
        "UPDATE email_inboxes SET deleted_at = ?, updated_at = ? WHERE id = ?",
        (now, now, str(inbox_id)),
    )


# ---------------------------------------------------------------------------
# Send / Reply
# ---------------------------------------------------------------------------


@router.post("/inboxes/{inbox_id}/send", status_code=status.HTTP_201_CREATED)
async def send_email(
    inbox_id: UUID,
    payload: EmailSendRequest,
    database: Database = Depends(get_database),
) -> dict[str, Any]:
    """Send a new email from a registered inbox."""
    from cyborg_server.config import Settings
    from cyborg_server.services.agentmail_client import AgentMailClient

    inbox = await database.fetch_one(
        "SELECT * FROM email_inboxes WHERE id = ? AND deleted_at IS NULL AND is_active = 1",
        (str(inbox_id),),
    )
    if inbox is None:
        raise HTTPException(status_code=404, detail="Inbox not found or inactive")

    settings = getattr(database, "settings", None)
    if not isinstance(settings, Settings):
        settings = Settings.from_env()

    async with AgentMailClient(
        base_url=settings.agentmail.base_url,
        api_key=settings.agentmail.api_key,
    ) as client:
        result = await client.send_message(
            inbox["agentmail_inbox_id"],
            to=payload.to,
            subject=payload.subject,
            text=payload.text,
            html=payload.html,
            cc=payload.cc,
        )
    return result


@router.post("/inboxes/{inbox_id}/messages/{message_id}/reply", status_code=status.HTTP_201_CREATED)
async def reply_to_email(
    inbox_id: UUID,
    message_id: str,
    payload: EmailReplyRequest,
    database: Database = Depends(get_database),
) -> dict[str, Any]:
    """Reply to an email message."""
    from cyborg_server.config import Settings
    from cyborg_server.services.agentmail_client import AgentMailClient

    inbox = await database.fetch_one(
        "SELECT * FROM email_inboxes WHERE id = ? AND deleted_at IS NULL AND is_active = 1",
        (str(inbox_id),),
    )
    if inbox is None:
        raise HTTPException(status_code=404, detail="Inbox not found or inactive")

    settings = getattr(database, "settings", None)
    if not isinstance(settings, Settings):
        settings = Settings.from_env()

    async with AgentMailClient(
        base_url=settings.agentmail.base_url,
        api_key=settings.agentmail.api_key,
    ) as client:
        result = await client.reply_message(
            inbox["agentmail_inbox_id"],
            message_id,
            text=payload.text,
            html=payload.html,
            reply_all=payload.reply_all,
        )
    return result


# ---------------------------------------------------------------------------
# Messages (proxy to AgentMail)
# ---------------------------------------------------------------------------


@router.get("/inboxes/{inbox_id}/messages")
async def list_messages(
    inbox_id: UUID,
    limit: int = Query(default=25, ge=1, le=100),
    page_token: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict[str, Any]:
    """List messages in an inbox (proxied to AgentMail)."""
    from cyborg_server.config import Settings
    from cyborg_server.services.agentmail_client import AgentMailClient

    inbox = await database.fetch_one(
        "SELECT * FROM email_inboxes WHERE id = ? AND deleted_at IS NULL",
        (str(inbox_id),),
    )
    if inbox is None:
        raise HTTPException(status_code=404, detail="Inbox not found")

    settings = getattr(database, "settings", None)
    if not isinstance(settings, Settings):
        settings = Settings.from_env()

    async with AgentMailClient(
        base_url=settings.agentmail.base_url,
        api_key=settings.agentmail.api_key,
    ) as client:
        return await client.list_messages(
            inbox["agentmail_inbox_id"],
            limit=limit,
            page_token=page_token,
        )


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------


@router.get("/threads", response_model=list[EmailThreadResponse])
async def list_threads(
    inbox_id: UUID | None = Query(default=None),
    active_only: bool = Query(default=True),
    database: Database = Depends(get_database),
) -> list[EmailThreadResponse]:
    """List tracked email threads."""
    query = "SELECT * FROM email_threads WHERE deleted_at IS NULL"
    params: list[Any] = []
    if inbox_id is not None:
        query += " AND inbox_id = ?"
        params.append(str(inbox_id))
    if active_only:
        query += " AND is_active = 1"
    query += " ORDER BY last_message_at DESC NULLS LAST"
    rows = await database.fetch_all(query, tuple(params))
    return [_row_to_thread(row) for row in rows]


@router.get("/threads/{thread_id}", response_model=EmailThreadResponse)
async def get_thread(
    thread_id: UUID,
    database: Database = Depends(get_database),
) -> EmailThreadResponse:
    """Get a tracked email thread."""
    row = await database.fetch_one(
        "SELECT * FROM email_threads WHERE id = ? AND deleted_at IS NULL",
        (str(thread_id),),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return _row_to_thread(row)
