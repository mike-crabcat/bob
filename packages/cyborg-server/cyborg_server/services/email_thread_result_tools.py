"""Email thread result tools — finish_email_thread for dispatching results to originating sessions."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from cyborg_server.services.tools import tool

if TYPE_CHECKING:
    from cyborg_server.context import AppContext
    from cyborg_server.services.whatsapp_bridge_service import WhatsAppBridgeService

logger = logging.getLogger(__name__)


def make_email_thread_result_tools(
    ctx: AppContext,
    *,
    thread_id: str,
    origin_session_key: str,
    agenda: str,
    wa_service: WhatsAppBridgeService | None = None,
) -> list:
    """Create the finish_email_thread tool for an email thread with an origin session.

    When called, dispatches the result back to the originating session.
    """

    @tool
    async def finish_email_thread(result: str) -> str:
        """Complete the email thread task and relay the result back to the requesting session.
        Call when you have achieved the objective from the email conversation.
        The result will be dispatched to the originating session, which will decide
        how to relay it."""
        from cyborg_server.services.thread_result_service import dispatch_thread_result

        db = ctx.db

        # Look up thread metadata for context
        thread_row = await db.fetch_one(
            "SELECT subject, contact_id FROM email_threads WHERE id = ? OR agentmail_thread_id = ?",
            (thread_id, thread_id),
        )
        subject = thread_row["subject"] if thread_row else "unknown"
        contact_name = "unknown"
        if thread_row and thread_row.get("contact_id"):
            contact = await db.fetch_one(
                "SELECT name FROM contacts WHERE id = ? AND deleted_at IS NULL",
                (thread_row["contact_id"],),
            )
            if contact:
                contact_name = contact["name"]

        # Clear origin_session_key from thread
        await db.execute(
            "UPDATE email_threads SET origin_session_key = NULL WHERE id = ? OR agentmail_thread_id = ?",
            (thread_id, thread_id),
        )

        result_content = (
            f"## Email Thread Result\n"
            f"Subject: {subject}\n"
            f"Contact: {contact_name}\n"
            f"Agenda: {agenda}\n\n"
            f"{result}"
        )

        await dispatch_thread_result(
            ctx,
            origin_session_key=origin_session_key,
            result_content=result_content,
            call_category="email_thread_result",
            wa_service=wa_service,
        )

        logger.info(
            "Email thread result dispatched from thread %s to origin session %s",
            thread_id, origin_session_key,
        )

        return json.dumps({"ok": True, "dispatched_to": origin_session_key})

    return [finish_email_thread]
