"""Dashboard email routes."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from cyborg_server.database import Database
from cyborg_server.dependencies import get_database

from ._helpers import _get_pending_approval_count, _render_template

router = APIRouter()


@router.get("/emails", response_class=HTMLResponse)
async def email_threads(
    request: Request,
    db: Database = Depends(get_database),
) -> Response:
    pending_count = await _get_pending_approval_count(db)

    threads = await db.fetch_all(
        """
        SELECT et.*, c.email as contact_email, ei.email_address as inbox_email
        FROM email_threads et
        LEFT JOIN contacts c ON c.id = et.contact_id AND c.deleted_at IS NULL
        LEFT JOIN email_inboxes ei ON ei.id = et.inbox_id AND ei.deleted_at IS NULL
        WHERE et.deleted_at IS NULL
        ORDER BY et.last_message_at DESC
        """,
    )

    inbox_count = await db.fetch_one(
        "SELECT COUNT(*) as cnt FROM email_inboxes WHERE deleted_at IS NULL AND is_active = 1",
    )
    active_count = sum(1 for t in threads if t.get("is_active"))

    thread_rows = []
    for t in threads:
        thread_rows.append({
            "id": t["id"],
            "subject": t.get("subject"),
            "agenda": t.get("agenda"),
            "contact_email": t.get("contact_email"),
            "message_count": int(t.get("message_count") or 0),
            "last_message_at": t.get("last_message_at"),
            "is_active": bool(t.get("is_active")),
        })

    all_messages = await db.fetch_all(
        """
        SELECT em.id, em.subject, em.sender_email, em.sender_name,
               em.to_addresses, em.message_timestamp, em.has_attachments,
               em.preview, em.thread_id, et.id as thread_row_id
        FROM email_messages em
        JOIN email_threads et ON et.agentmail_thread_id = em.thread_id
        ORDER BY em.message_timestamp ASC
        """,
    )

    messages_by_thread: dict[str, list] = {}
    for m in all_messages:
        tid = m["thread_row_id"]
        row = dict(m)
        row["to_addresses"] = json.loads(m.get("to_addresses") or "[]")
        messages_by_thread.setdefault(tid, []).append(row)

    return _render_template("dashboard/emails.html", request, {
        "pending_count": pending_count,
        "threads": thread_rows,
        "active_count": active_count,
        "inbox_count": inbox_count["cnt"] if inbox_count else 0,
        "messages_by_thread": messages_by_thread,
    })


@router.get("/emails/{message_id}", response_class=HTMLResponse)
async def email_message_detail(
    message_id: str,
    request: Request,
    db: Database = Depends(get_database),
) -> Response:
    pending_count = await _get_pending_approval_count(db)

    msg = await db.fetch_one(
        """
        SELECT em.*, ei.email_address as inbox_email
        FROM email_messages em
        LEFT JOIN email_inboxes ei ON ei.id = em.inbox_id AND ei.deleted_at IS NULL
        WHERE em.id = :id
        """,
        {"id": message_id},
    )
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")

    to_addresses = json.loads(msg.get("to_addresses") or "[]")
    cc_addresses = json.loads(msg.get("cc_addresses") or "[]")
    attachments = json.loads(msg.get("attachments_json") or "[]")

    thread_row = await db.fetch_one(
        "SELECT session_key FROM email_threads WHERE agentmail_thread_id = ? AND deleted_at IS NULL",
        (msg["thread_id"],),
    )
    dispatches = []
    prompt_history = []
    if thread_row and thread_row["session_key"]:
        session_key = thread_row["session_key"]

        dispatch_rows = await db.fetch_all(
            """
            SELECT d.*, t.title AS task_title, p.title AS project_title
            FROM dispatches d
            LEFT JOIN tasks t ON t.id = d.task_id AND t.deleted_at IS NULL
            LEFT JOIN projects p ON p.id = d.project_id AND p.deleted_at IS NULL
            WHERE d.session_key = ?
            ORDER BY d.dispatched_at DESC
            """,
            (session_key,),
        )
        for d in dispatch_rows:
            dispatches.append({
                "id": d["id"],
                "notification_type": d["notification_type"],
                "status": d["status"],
                "task_id": d["task_id"],
                "task_title": d["task_title"],
                "project_title": d["project_title"],
                "dispatched_at": d["dispatched_at"],
                "completed_at": d["completed_at"],
                "duration_seconds": d["duration_seconds"],
                "tap_count": d["tap_count"],
            })

        prompt_rows = await db.fetch_all(
            """
            SELECT id, category, prompt_text, session_key, timestamp, token_count_estimate
            FROM prompt_history
            WHERE session_key = ?
            ORDER BY timestamp DESC
            LIMIT 20
            """,
            (session_key,),
        )
        for p in prompt_rows:
            prompt_history.append({
                "id": p["id"],
                "category": p["category"],
                "prompt_text": p["prompt_text"],
                "timestamp": p["timestamp"],
                "token_count_estimate": p["token_count_estimate"],
            })

    return _render_template("dashboard/email_detail.html", request, {
        "pending_count": pending_count,
        "message": dict(msg),
        "to_addresses": to_addresses,
        "cc_addresses": cc_addresses,
        "attachments": attachments,
        "dispatches": dispatches,
        "prompt_history": prompt_history,
    })
