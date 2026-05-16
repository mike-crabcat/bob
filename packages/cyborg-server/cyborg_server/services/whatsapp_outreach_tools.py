"""WhatsApp outreach tools — proactive messaging and cross-session retrieval."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from cyborg_server.services.tools import tool

if TYPE_CHECKING:
    from cyborg_server.context import AppContext
    from cyborg_server.services.whatsapp_bridge_service import WhatsAppBridgeService

logger = logging.getLogger(__name__)


def _phone_to_jid(phone_number: str) -> str:
    """Convert a normalized phone number (+CCXXXXXXXXX) to a WhatsApp JID."""
    digits = re.sub(r"\D", "", phone_number)
    return f"{digits}@s.whatsapp.net"


def make_whatsapp_outreach_tools(
    ctx: AppContext,
    wa_service: WhatsAppBridgeService,
    current_session_key: str,
) -> list:
    """Create outreach tools for a trusted WhatsApp DM session.

    Tools: search_contacts, send_whatsapp_to_contact, get_contact_session_messages.
    Only injected for trusted contacts in DM sessions.
    """

    @tool
    async def search_contacts(query: str, limit: int = 5) -> str:
        """Search contacts by name, phone number, or email.
        Returns matching contacts with their ID, name, phone, and trusted status."""
        from cyborg_server.services.base import BaseService
        db = ctx.db
        pattern = f"%{query}%"
        rows = await db.fetch_all(
            """
            SELECT id, name, phone_number, email, is_trusted
            FROM contacts
            WHERE deleted_at IS NULL
              AND (name LIKE ? OR phone_number LIKE ? OR email LIKE ?)
            ORDER BY name
            LIMIT ?
            """,
            (pattern, pattern, pattern, limit),
        )
        results = [
            {
                "id": row["id"],
                "name": row["name"],
                "phone_number": row["phone_number"],
                "email": row.get("email"),
                "is_trusted": bool(row.get("is_trusted", 0)),
            }
            for row in rows
        ]
        return json.dumps(results)

    @tool
    async def send_whatsapp_to_contact(
        contact_id: str,
        message: str,
        purpose: str,
    ) -> str:
        """Send a WhatsApp message to a trusted contact (not the current chat).
        The contact must be trusted. The 'purpose' describes why you're reaching out,
        which helps handle the reply."""
        from cyborg_server.exceptions import ConflictError
        from cyborg_server.models import SessionRouteCreate, SessionRouteKind
        from cyborg_server.services.session_agenda_service import (
            SessionAgendaService,
            WHATSAPP_OUTREACH_AGENDA_TEMPLATE,
        )
        from cyborg_server.services.session_route_service import SessionRouteService
        from cyborg_server.services.session_service import SessionService

        db = ctx.db

        # Look up contact and validate trust
        contact = await db.fetch_one(
            "SELECT id, name, phone_number, is_trusted FROM contacts WHERE id = ? AND deleted_at IS NULL",
            (contact_id,),
        )
        if contact is None:
            return json.dumps({"ok": False, "error": "Contact not found"})
        if not bool(contact.get("is_trusted", 0)):
            return json.dumps({"ok": False, "error": "Contact is not trusted"})

        phone = contact["phone_number"]
        if not phone:
            return json.dumps({"ok": False, "error": "Contact has no phone number"})

        # Check bridge connectivity
        if not wa_service.connected:
            return json.dumps({"ok": False, "error": "WhatsApp bridge is not connected"})

        # Convert phone to JID and send
        jid = _phone_to_jid(phone)
        request_id = await wa_service.send_message(jid, message)

        # Derive session key for the target contact
        phone_digits = re.sub(r"\D", "", phone)
        target_session_key = f"agent:main:whatsapp:dm:{phone_digits}"

        # Create session route for target contact's DM
        route_service = SessionRouteService(ctx)
        try:
            await route_service.create_route(SessionRouteCreate(
                channel="whatsapp",
                session_key=target_session_key,
                kind=SessionRouteKind.DM,
                contact_id=contact["id"],
                metadata={"outreach_initiated_from": current_session_key},
            ))
        except ConflictError:
            pass  # Route already exists

        # Set outreach agenda on target session
        # Derive requestor name from current session context
        requestor_name = "the agent"
        current_route = await db.fetch_one(
            "SELECT contact_id FROM session_routes WHERE session_key = ?",
            (current_session_key,),
        )
        if current_route and current_route.get("contact_id"):
            requestor = await db.fetch_one(
                "SELECT name FROM contacts WHERE id = ?",
                (current_route["contact_id"],),
            )
            if requestor:
                requestor_name = requestor["name"]

        outreach_agenda = WHATSAPP_OUTREACH_AGENDA_TEMPLATE.format(
            requestor_name=requestor_name,
            purpose=purpose,
        )
        agenda_service = SessionAgendaService(ctx)
        existing_agenda = await agenda_service.get_agenda(target_session_key) or ""
        combined_agenda = f"{existing_agenda}\n\n{outreach_agenda}" if existing_agenda else outreach_agenda
        await agenda_service.set_agenda(target_session_key, combined_agenda)

        # Store the outreach turn in the target DM session so it appears in
        # conversation history when the contact replies.
        session_service = SessionService(ctx)
        user_context = (
            f"[Outreach initiated by {requestor_name}] "
            f"Purpose: {purpose}"
        )
        await session_service.add_message(
            target_session_key, "user", user_context,
            channel="whatsapp",
            metadata={"outreach": True, "purpose": purpose, "requestor": requestor_name},
        )
        await session_service.add_message(
            target_session_key, "assistant", message,
            channel="whatsapp",
            metadata={"outreach": True, "purpose": purpose, "requestor": requestor_name},
        )

        # Upsert the contact as a participant in the target session
        from cyborg_server.services.base import utcnow
        now_iso = utcnow().isoformat()
        await db.execute(
            """INSERT INTO session_participants (session_key, identifier, display_name, contact_id, is_trusted, last_active_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_key, identifier) DO UPDATE SET
                   display_name = excluded.display_name,
                   contact_id = COALESCE(excluded.contact_id, session_participants.contact_id),
                   is_trusted = CASE WHEN excluded.contact_id IS NOT NULL THEN excluded.is_trusted ELSE session_participants.is_trusted END,
                   last_active_at = excluded.last_active_at""",
            (target_session_key, phone, contact["name"],
             contact["id"], 1, now_iso),
        )

        logger.info(
            "Outreach sent to %s (%s) session=%s request=%s",
            contact["name"], phone, target_session_key, request_id,
        )

        # Record in llm_call_log so the session appears in the dashboard
        from uuid import uuid4
        from cyborg_server.services.llm_dispatch import _record_log
        await _record_log(
            db,
            provider="outreach",
            model="",
            call_category="whatsapp_outreach",
            session_key=target_session_key,
            user_message=f"[Outreach to {contact['name']}] {purpose}",
            response_text=message,
            status="completed",
        )

        return json.dumps({
            "ok": True,
            "contact_name": contact["name"],
            "request_id": request_id,
        })

    @tool
    async def get_contact_session_messages(
        contact_name: str,
        limit: int = 10,
    ) -> str:
        """Retrieve recent messages from a contact's WhatsApp session.
        Use this to check if a contact has replied to an outreach message."""
        from cyborg_server.services.session_service import SessionService

        db = ctx.db

        # Look up contact by name
        contact = await db.fetch_one(
            "SELECT id, name, phone_number FROM contacts WHERE name LIKE ? AND deleted_at IS NULL LIMIT 1",
            (f"%{contact_name}%",),
        )
        if contact is None:
            return json.dumps({"ok": False, "error": f"No contact found matching '{contact_name}'"})

        phone_digits = re.sub(r"\D", "", contact["phone_number"])
        target_session_key = f"agent:main:whatsapp:dm:{phone_digits}"

        session_service = SessionService(ctx)
        messages = await session_service.get_messages(target_session_key, limit=limit)

        if not messages:
            return json.dumps({
                "ok": True,
                "contact_name": contact["name"],
                "messages": [],
                "note": "No messages found in this session yet.",
            })

        return json.dumps({
            "ok": True,
            "contact_name": contact["name"],
            "messages": [
                {
                    "role": m.role,
                    "content": m.content,
                    "channel": m.channel,
                    "created_at": m.created_at,
                }
                for m in messages
            ],
        })

    return [search_contacts, send_whatsapp_to_contact, get_contact_session_messages]
