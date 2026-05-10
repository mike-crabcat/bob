"""Email tools for LLM function calling."""

from __future__ import annotations

import json
import logging

from cyborg_server.context import AppContext
from cyborg_server.services.tools import tool

logger = logging.getLogger(__name__)


def make_email_tools(ctx: AppContext, thread_id: str, inbox_id: str):
    """Create email reply/skip tools bound to the given thread."""

    @tool
    async def email_reply(body: str) -> str:
        """Send a reply to the current email thread."""
        from cyborg_server.services.email_delivery_service import EmailDeliveryService

        svc = EmailDeliveryService(ctx)
        try:
            result = await svc.send_reply(inbox_id=inbox_id, thread_id=thread_id, text=body)
            return json.dumps({"ok": True, "thread_id": thread_id})
        except Exception as e:
            logger.warning("email_reply failed: %s", e)
            return f"Error sending reply: {e}"

    @tool
    async def email_skip() -> str:
        """Skip replying to this email — no response is needed."""
        return json.dumps({"ok": True, "skipped": True})

    return [email_reply, email_skip]
