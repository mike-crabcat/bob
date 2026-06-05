"""Email tools for LLM function calling."""

from __future__ import annotations

import json
import logging

from cyborg_server.context import AppContext
from cyborg_server.services.tools import Tool, tool

logger = logging.getLogger(__name__)


def make_email_tools(ctx: AppContext, thread_id: str, inbox_id: str, *, reply_tracker: list | None = None):
    """Create email reply/skip tools bound to the given thread.

    If reply_tracker is provided, email_reply sets tracker[0] = True
    so callers can detect whether a reply was sent.
    """

    @tool
    async def email_reply(body: str) -> str:
        """Send a reply to the current email thread. Always use this tool to respond — do not just generate text output."""
        from cyborg_server.services.email_delivery_service import EmailDeliveryService

        svc = EmailDeliveryService(ctx)
        try:
            result = await svc.send_reply(inbox_id=inbox_id, thread_id=thread_id, text=body)
            if reply_tracker is not None:
                reply_tracker[0] = True
            return json.dumps({"ok": True, "thread_id": thread_id})
        except Exception as e:
            logger.warning("email_reply failed: %s", e)
            return f"Error sending reply: {e}"

    @tool
    async def email_skip() -> str:
        """Skip replying to this email — no response is needed."""
        return json.dumps({"ok": True, "skipped": True})

    return [email_reply, email_skip]


def make_email_send_tools(ctx: AppContext, *, session_key: str | None = None) -> list[Tool]:
    """Create email_send tool for initiating new email threads. Not bound to a specific thread."""

    @tool
    async def email_send(
        to: str,
        subject: str,
        body: str,
        agenda: str,
    ) -> str:
        """Send a new email to start a conversation with someone. Use this to proactively reach out to a contact by email (follow up, schedule, begin a discussion). The agenda describes the purpose and guides all future responses in this thread. The recipient email address must be known."""
        from cyborg_server.services.email_delivery_service import EmailDeliveryService

        # Resolve default inbox
        inbox = await ctx.db.fetch_one(
            "SELECT id FROM email_inboxes WHERE deleted_at IS NULL AND is_active = 1 LIMIT 1",
        )
        if inbox is None:
            return "Error: no active email inbox configured"

        try:
            svc = EmailDeliveryService(ctx)
            result = await svc.send_new_email(
                inbox_id=inbox["id"],
                to=to,
                subject=subject,
                text=body,
                agenda=agenda,
                origin_session_key=session_key,
            )
            return json.dumps({"ok": True, "thread_id": result.get("thread_id", "")})
        except Exception as e:
            logger.warning("email_send failed: %s", e)
            return f"Error sending email: {e}"

    return [email_send]


def make_email_thread_tools(
    ctx: AppContext, *, contact_id: str | None = None, is_trusted: bool = False,
) -> list[Tool]:
    """Create email thread tools (read + search).

    Args:
        contact_id: The current session's contact. Used to scope search results
            for untrusted contacts to only their own threads.
        is_trusted: If True, search returns all threads. If False, only threads
            belonging to contact_id.
    """

    @tool
    async def email_thread_read(thread_id: str) -> str:
        """Read the full transcript of an email thread by its agentmail thread ID.
        Returns subject, participants, and all messages in chronological order.
        Use this to look up the original context behind an email-sourced memory bulletin."""
        thread = await ctx.db.fetch_one(
            "SELECT agentmail_thread_id, subject, inbox_id, contact_id, last_message_at "
            "FROM email_threads WHERE agentmail_thread_id = ? AND deleted_at IS NULL",
            (thread_id,),
        )
        if thread is None:
            return json.dumps({"error": f"Thread not found: {thread_id}"})

        messages = await ctx.db.fetch_all(
            "SELECT sender_email, sender_name, subject, text_body, message_timestamp "
            "FROM email_messages WHERE thread_id = ? ORDER BY message_timestamp ASC",
            (thread_id,),
        )

        # Resolve inbox address to detect outbound messages
        inbox = await ctx.db.fetch_one(
            "SELECT email_address FROM email_inboxes WHERE id = ?",
            (thread["inbox_id"],),
        )
        inbox_email = (inbox["email_address"] or "").lower() if inbox else ""

        lines = []
        for msg in messages:
            text = (msg.get("text_body") or "").strip()
            if not text:
                continue
            sender_email = (msg.get("sender_email") or "").lower()
            sender_name = msg.get("sender_name") or msg.get("sender_email") or "Unknown"
            subject = msg.get("subject", "(no subject)")
            ts = msg.get("message_timestamp", "")
            role = "assistant" if sender_email == inbox_email else sender_name
            lines.append(f"[{ts}] [{role}] [Subject: {subject}]\n{text}")

        return json.dumps({
            "thread_id": thread["agentmail_thread_id"],
            "subject": thread.get("subject", ""),
            "contact_id": thread.get("contact_id"),
            "message_count": len(messages),
            "transcript": "\n\n".join(lines),
        })

    @tool
    async def email_thread_search(query: str) -> str:
        """Search email threads by keyword. Returns a ranked list of matching threads
        with thread_id, subject, contact name, message count, and last message date.
        Use email_thread_read with the thread_id to get the full transcript."""
        import re

        terms = [t for t in re.split(r"\s+", query.strip()) if t]
        if not terms:
            return json.dumps({"error": "Empty query", "results": []})

        # Build WHERE clause for text search across subjects and message bodies
        # Each term must match in subject or any message body
        msg_conditions = " OR ".join(
            "(em.text_body LIKE ? OR em.subject LIKE ?)" for _ in terms
        )
        msg_params = []
        for t in terms:
            like = f"%{t}%"
            msg_params.extend([like, like])

        # Scope: untrusted contacts only see their own threads
        scope_clause = ""
        scope_params: list[str] = []
        if not is_trusted and contact_id:
            scope_clause = "AND et.contact_id = ?"
            scope_params.append(contact_id)

        # Find threads where the subject matches or any message matches
        sql = (
            "SELECT et.agentmail_thread_id, et.subject, et.contact_id, "
            "et.message_count, et.last_message_at, "
            "c.name as contact_name, "
            "COUNT(DISTINCT em.id) as matching_messages, "
            "CASE WHEN et.subject LIKE ? THEN 1 ELSE 0 END as subject_match "
            "FROM email_threads et "
            "LEFT JOIN email_messages em ON em.thread_id = et.agentmail_thread_id "
            f"AND ({msg_conditions}) "
            "LEFT JOIN contacts c ON c.id = et.contact_id "
            f"WHERE et.deleted_at IS NULL AND et.is_active = 1 {scope_clause} "
            "GROUP BY et.agentmail_thread_id "
            "HAVING matching_messages > 0 OR subject_match = 1 "
            "ORDER BY subject_match DESC, matching_messages DESC, et.last_message_at DESC "
            "LIMIT 20"
        )

        # Subject match term (first term for ranking)
        subject_like = f"%{terms[0]}%"

        params = [subject_like] + msg_params + scope_params

        rows = await ctx.db.fetch_all(sql, tuple(params))

        results = []
        for row in rows:
            results.append({
                "thread_id": row["agentmail_thread_id"],
                "subject": row.get("subject", ""),
                "contact_name": row.get("contact_name"),
                "message_count": row.get("message_count", 0),
                "matching_messages": row.get("matching_messages", 0),
                "last_message_at": row.get("last_message_at"),
            })

        return json.dumps({"query": query, "result_count": len(results), "results": results})

    return [email_thread_read, email_thread_search]
