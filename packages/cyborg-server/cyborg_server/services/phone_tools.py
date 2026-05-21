"""Phone call tools — LLM-initiated outbound calls via Twilio."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from cyborg_server.services.tools import tool

if TYPE_CHECKING:
    from cyborg_server.context import AppContext

logger = logging.getLogger(__name__)


def make_phone_tools(
    ctx: AppContext,
) -> list:
    """Create phone call tools for the LLM agent.

    Tools: make_phone_call, get_call_status.
    """

    @tool
    async def make_phone_call(
        agenda: str,
        contact_id: str | None = None,
        phone_number: str | None = None,
    ) -> str:
        """Initiate an outbound phone call. Provide either a contact_id (to look up
        their phone number) or a phone_number directly in E.164 format (e.g. +61400123456).
        The agenda describes what the call is about and guides the AI agent during the conversation."""
        if not contact_id and not phone_number:
            return json.dumps({"ok": False, "error": "Provide either contact_id or phone_number"})

        db = ctx.db
        phone_settings = ctx.settings.phone
        if not phone_settings.enabled:
            return json.dumps({"ok": False, "error": "Phone subsystem is not enabled"})

        to_number = phone_number

        if contact_id:
            contact = await db.fetch_one(
                "SELECT id, name, phone_number FROM contacts WHERE id = ? AND deleted_at IS NULL",
                (contact_id,),
            )
            if contact is None:
                return json.dumps({"ok": False, "error": "Contact not found"})
            if not contact["phone_number"]:
                return json.dumps({"ok": False, "error": "Contact has no phone number"})
            to_number = contact["phone_number"]

        if not to_number:
            return json.dumps({"ok": False, "error": "No phone number to call"})

        # Check for active call to the same number
        active = await db.fetch_one(
            "SELECT id FROM phone_calls WHERE phone_number = ? AND status NOT IN ('completed', 'failed', 'busy', 'no-answer', 'canceled')",
            (to_number,),
        )
        if active:
            return json.dumps({"ok": False, "error": f"Active call already in progress to {to_number}"})

        from cyborg_server.routers.phone import initiate_outbound_call

        result = await initiate_outbound_call(
            db=db,
            settings=ctx.settings,
            phone_settings=phone_settings,
            to_number=to_number,
            agenda=agenda,
            app_state=None,
        )

        if "error" in result:
            return json.dumps({"ok": False, "error": result["error"]})

        return json.dumps({
            "ok": True,
            "call_id": result["call_id"],
            "call_sid": result["call_sid"],
            "status": result["status"],
            "phone_number": to_number,
        })

    @tool
    async def get_call_status(call_id: str) -> str:
        """Check the status of a phone call. Returns current status, duration, and exchange count."""
        db = ctx.db
        call = await db.fetch_one(
            """SELECT id, call_sid, phone_number, direction, status, agenda,
                      exchange_count, duration_seconds, started_at, completed_at
               FROM phone_calls WHERE id = ? OR call_sid = ?""",
            (call_id, call_id),
        )
        if not call:
            return json.dumps({"ok": False, "error": "Call not found"})
        return json.dumps({"ok": True, **dict(call)})

    return [make_phone_call, get_call_status]
