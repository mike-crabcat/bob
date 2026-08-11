"""Phone call tools — LLM-initiated outbound calls via Twilio."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from bob_server.services.tools import tool

if TYPE_CHECKING:
    from bob_server.context import AppContext

logger = logging.getLogger(__name__)


def make_phone_tools(
    ctx: AppContext,
    *,
    session_key: str | None = None,
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

        from bob_server.routers.phone import initiate_outbound_call

        result = await initiate_outbound_call(
            db=db,
            settings=ctx.settings,
            phone_settings=phone_settings,
            to_number=to_number,
            agenda=agenda,
            app_state=None,
            origin_session_key=session_key,
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

    @tool
    async def place_realtime_call(
        instructions: str,
        contact_id: str | None = None,
        phone_number: str | None = None,
        voice: str | None = None,
        max_duration_seconds: int | None = None,
    ) -> str:
        """Place an outbound PHONE call (Twilio) handled by OpenAI Realtime voice AI.

        This rings the contact's actual phone — costs money, needs Twilio credentials.
        For a browser voice link (no phone call, free, the contact taps a link in their
        DM), use reach_out_with_voice_call(modality="voice_link") instead — that's the
        right default for WhatsApp contacts and for "VoIP"/"realtime"/"browser call" requests.

        instructions = the system prompt guiding the voice agent for this call: the goal,
        the context it needs (party size, times, names, fallback windows), how to handle
        ambiguity, and hard rules (never give payment details, etc.). The agent cannot ask
        you mid-call — anything not in the instructions, it must improvise or refuse.

        The voice agent has these generic tools: get_caller_details, report_success,
        report_failure, end_call. The outcome tools are task-agnostic — your instructions
        tell the agent what facts to capture in `details` (booking time, appointment ID,
        confirmation number — whatever matters for the specific task). Tell the instructions
        when to use each tool, e.g. "the moment they confirm, call report_success with
        summary and these detail keys" and "if you can't complete it, call report_failure
        with the reason, then end_call".

        For a worked example (restaurant booking), see
        workspace/realtime_prompts/restaurant_booking.md — compose similar bounded prompts
        for any other task type (appointment reschedule, delivery confirmation, RSVP, etc.).

        Provide either contact_id or phone_number (E.164). Only call numbers the user has
        authorized — outbound robocalls carry consent obligations.
        """
        if not contact_id and not phone_number:
            return json.dumps({"ok": False, "error": "Provide either contact_id or phone_number"})

        phone_settings = ctx.settings.phone
        if not phone_settings.enabled:
            return json.dumps({"ok": False, "error": "Phone subsystem is not enabled"})

        to_number = phone_number
        if contact_id:
            contact = await ctx.db.fetch_one(
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

        active = await ctx.db.fetch_one(
            "SELECT id FROM phone_calls WHERE phone_number = ? AND status NOT IN ('completed', 'failed', 'busy', 'no-answer', 'canceled')",
            (to_number,),
        )
        if active:
            return json.dumps({"ok": False, "error": f"Active call already in progress to {to_number}"})

        # Track as a subagent so the result flows back through the usual channels.
        from bob_server.services.subagent_service import SubagentService
        sub = await SubagentService(ctx).create_subagent(
            task=instructions,
            parent_session_key=session_key or "agent:main:phone",
            agent_type="openai_voice",
        )
        subagent_id = sub["subagent_id"]

        from bob_server.routers.phone import initiate_outbound_call

        result = await initiate_outbound_call(
            db=ctx.db,
            settings=ctx.settings,
            phone_settings=phone_settings,
            to_number=to_number,
            agenda=instructions,
            app_state=None,
            origin_session_key=session_key,
            engine="openai_realtime",
            realtime_meta={
                "instructions": instructions,
                "voice": voice or "",
                "max_duration": max_duration_seconds,
                "subagent_id": subagent_id,
            },
        )

        if "error" in result:
            return json.dumps({"ok": False, "error": result["error"]})

        return json.dumps({
            "ok": True,
            "subagent_id": subagent_id,
            "call_id": result["call_id"],
            "call_sid": result["call_sid"],
            "status": result["status"],
            "phone_number": to_number,
        })

    return [make_phone_call, get_call_status, place_realtime_call]
