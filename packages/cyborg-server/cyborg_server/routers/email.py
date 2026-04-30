"""HTTP routes for email relay via AgentMail."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import logging

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
logger = logging.getLogger(__name__)


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
        agenda=row.get("agenda"),
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
    from cyborg_server.services.email_polling_service import (
        CUSTOM_AGENDA_TEMPLATE,
        resolve_or_create_email_thread,
    )
    from cyborg_server.services.openclaw_hook_service import OpenClawHookService

    inbox = await database.fetch_one(
        "SELECT * FROM email_inboxes WHERE id = ? AND deleted_at IS NULL AND is_active = 1",
        (str(inbox_id),),
    )
    if inbox is None:
        raise HTTPException(status_code=404, detail="Inbox not found or inactive")

    settings = getattr(database, "settings", None)
    if not isinstance(settings, Settings):
        settings = Settings.from_env()

    # Send via AgentMail
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
            attachments=[a.model_dump(exclude_none=True) for a in payload.attachments] if payload.attachments else None,
        )

    agentmail_message_id = result.get("message_id", "")
    agentmail_thread_id = result.get("thread_id", "")

    if not agentmail_thread_id:
        return result

    # Look up contact from recipient
    contact_id = None
    recipient_email = payload.to if isinstance(payload.to, str) else (payload.to[0] if payload.to else "")
    if recipient_email:
        contact = await database.fetch_one(
            "SELECT id FROM contacts WHERE email = ? AND deleted_at IS NULL LIMIT 1",
            (recipient_email,),
        )
        if contact:
            contact_id = contact["id"]

    # Create thread + session route (or find existing)
    thread, is_new_thread = await resolve_or_create_email_thread(
        database,
        inbox=inbox,
        agentmail_thread_id=agentmail_thread_id,
        subject=payload.subject,
        contact_id=contact_id,
        agenda=payload.agenda,
    )

    # Store outgoing message record
    now = utcnow()
    message_id = str(uuid4())
    await database.execute(
        """
        INSERT INTO email_messages (
            id, inbox_id, agentmail_message_id, thread_id,
            subject, sender_email, sender_name,
            to_addresses, cc_addresses,
            text_body, html_body, preview, labels,
            has_attachments, in_reply_to,
            message_timestamp, processed_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            inbox["id"],
            agentmail_message_id,
            agentmail_thread_id,
            payload.subject,
            inbox["email_address"],
            inbox["display_name"],
            json_dumps([payload.to] if isinstance(payload.to, str) else payload.to),
            json_dumps(payload.cc or []),
            payload.text,
            payload.html,
            payload.text[:200] if payload.text else None,
            json_dumps(["sent"]),
            1 if payload.attachments else 0,
            None,
            now.isoformat(),
            now.isoformat(),
            now.isoformat(),
        ),
    )

    # Update thread message count
    await database.execute(
        """
        UPDATE email_threads
        SET message_count = message_count + 1, last_message_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (now.isoformat(), now.isoformat(), thread["id"]),
    )

    # Dispatch to OpenClaw
    if settings.openclaw.enabled:
        hook_service = OpenClawHookService(
            database,
            cyborg_service_url=settings.resolved_public_url,
        )

        # Combined dispatch: agenda + outgoing email context
        send_parts: list[str] = []

        if is_new_thread:
            send_parts.append(CUSTOM_AGENDA_TEMPLATE.format(
                agenda=payload.agenda, inbox_id=inbox["id"],
            ))
            send_parts.append("")

        send_parts += [
            "## Email You Just Sent",
            "This is provided for your context. Do NOT reply — wait for the recipient to respond.",
            "",
            f"Subject: {payload.subject}",
            f"To: {payload.to}",
            "",
            payload.text,
        ]

        send_prompt = "\n".join(send_parts)
        send_key = f"email:send:{agentmail_message_id}"

        logger.info(
            "Dispatching send to OpenClaw session=%s idempotency=%s timeout=%ds new_thread=%s\n%s",
            thread["session_key"], send_key,
            int(settings.openclaw.timeout_seconds),
            is_new_thread,
            send_prompt,
        )
        await hook_service._send_gateway_request(
            "agent",
            {
                "message": send_prompt,
                "deliver": False,
                "sessionKey": thread["session_key"],
                "thinking": "on",
                "timeout": int(settings.openclaw.timeout_seconds),
                "idempotencyKey": send_key,
            },
        )
        logger.info("Send dispatch accepted for thread %s", thread["id"])

    return result


@router.post("/inboxes/{inbox_id}/reply", status_code=status.HTTP_201_CREATED)
async def reply_to_email(
    inbox_id: UUID,
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
            payload.message_id,
            text=payload.text,
            html=payload.html,
            reply_all=payload.reply_all,
            attachments=[a.model_dump(exclude_none=True) for a in payload.attachments] if payload.attachments else None,
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
# Attachments
# ---------------------------------------------------------------------------


@router.get("/inboxes/{inbox_id}/messages/{message_id}/attachments/{attachment_id}")
async def download_attachment(
    inbox_id: UUID,
    message_id: str,
    attachment_id: str,
    database: Database = Depends(get_database),
) -> Response:
    """Download an email attachment from AgentMail."""
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
        content = await client.get_attachment(
            inbox["agentmail_inbox_id"],
            message_id,
            attachment_id,
        )
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{attachment_id}"'},
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


@router.patch("/threads/{thread_id}/agenda", response_model=EmailThreadResponse)
async def update_thread_agenda(
    thread_id: UUID,
    payload: dict[str, str],
    database: Database = Depends(get_database),
) -> EmailThreadResponse:
    """Update the agenda for an email thread."""
    agenda = payload.get("agenda", "").strip()
    if not agenda:
        raise HTTPException(status_code=422, detail="agenda must not be empty")
    now_iso = utcnow().isoformat()
    await database.execute(
        "UPDATE email_threads SET agenda = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
        (agenda, now_iso, str(thread_id)),
    )
    row = await database.fetch_one(
        "SELECT * FROM email_threads WHERE id = ?",
        (str(thread_id),),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Thread not found")
    return _row_to_thread(row)
