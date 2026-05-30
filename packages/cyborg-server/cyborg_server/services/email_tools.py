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


def make_email_send_tools(ctx: AppContext) -> list[Tool]:
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
            )
            return json.dumps({"ok": True, "thread_id": result.get("thread_id", "")})
        except Exception as e:
            logger.warning("email_send failed: %s", e)
            return f"Error sending email: {e}"

    return [email_send]
