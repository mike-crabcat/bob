"""WhatsApp outreach tools — proactive messaging and cross-session retrieval."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING, Any
from uuid import uuid4

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

    Tools: send_whatsapp_to_contact, get_contact_session_messages.
    Only injected for trusted contacts in DM sessions.
    """

    @tool
    async def send_whatsapp_to_contact(
        contact_id: str,
        message: str,
        objective: str,
    ) -> str:
        """Send a WhatsApp message to a trusted contact (not the current chat).
        The contact must be trusted. The 'objective' describes the specific outcome
        you need from this conversation, e.g. "Find out if John can meet on Thursday
        and what time works." The target session will be instructed to work toward
        this objective and report back when complete."""
        from cyborg_server.exceptions import ConflictError
        from cyborg_server.models import SessionRouteCreate, SessionRouteKind
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

        # Create or update session route with outreach metadata
        outreach_meta = {
            "outreach_initiated_from": current_session_key,
            "outreach_objective": objective,
            "outreach_requestor": requestor_name,
            "outreach_message": message,
        }
        route_service = SessionRouteService(ctx)
        try:
            await route_service.create_route(SessionRouteCreate(
                channel="whatsapp",
                session_key=target_session_key,
                kind=SessionRouteKind.DM,
                contact_id=contact["id"],
                metadata=outreach_meta,
            ))
        except ConflictError:
            # Route exists — update metadata with outreach info
            existing = await db.fetch_one(
                "SELECT metadata FROM session_routes WHERE session_key = ?",
                (target_session_key,),
            )
            meta = {}
            if existing and existing["metadata"]:
                try:
                    meta = json.loads(existing["metadata"])
                except (json.JSONDecodeError, TypeError):
                    pass
            meta.update(outreach_meta)
            await db.execute(
                "UPDATE session_routes SET metadata = ? WHERE session_key = ?",
                (json.dumps(meta), target_session_key),
            )

        # Store the outreach message in target session history as assistant (cyborg sent it)
        session_service = SessionService(ctx)
        await session_service.add_message(
            target_session_key, "assistant", message,
            channel="whatsapp",
            metadata={"outreach": True, "objective": objective, "requestor": requestor_name},
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
            "Outreach sent to %s (%s) session=%s request=%s objective=%s",
            contact["name"], phone, target_session_key, request_id, objective[:80],
        )

        # Log under source session so it shows as cyborg-initiated
        from cyborg_server.services.llm_dispatch import _record_log
        await _record_log(
            db,
            provider="outreach",
            model="",
            call_category="whatsapp_outreach",
            session_key=current_session_key,
            user_message=f"Reach out to {contact['name']}: {objective}",
            response_text=message,
            status="completed",
            contact_id=contact["id"],
        )
        # Also log under target session so it surfaces in the dashboard
        await _record_log(
            db,
            provider="outreach",
            model="",
            call_category="whatsapp_outreach",
            session_key=target_session_key,
            user_message=f"[Outreach initiated — requested by {requestor_name}] {objective}",
            response_text=message,
            status="completed",
            contact_id=contact["id"],
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

    return [send_whatsapp_to_contact, get_contact_session_messages]


def _session_key_to_chat_id(session_key: str) -> str | None:
    """Derive a WhatsApp chat_id (JID) from a session key."""
    parts = session_key.split(":")
    if len(parts) < 5 or parts[2] != "whatsapp":
        return None
    kind = parts[3]
    ident = parts[4]
    if kind == "dm":
        return f"{ident}@s.whatsapp.net"
    if kind == "group":
        return f"{ident}@g.us"
    return None


def make_outreach_reply_tools(
    ctx: AppContext,
    wa_service: WhatsAppBridgeService,
    current_session_key: str,
) -> list:
    """Create the finish_outreach tool for an active outreach target session.

    When called, dispatches an LLM call in the source session to receive the result.
    """

    @tool
    async def finish_outreach(result: str) -> str:
        """Complete the active outreach request and relay the result back.
        Call when you have achieved the objective or obtained the requested information.
        The result will be dispatched to the originating session, which will decide
        how to handle it (potentially messaging the requesting contact)."""
        from cyborg_server.services.session_service import SessionService
        from cyborg_server.services.llm_dispatch import LLMDispatchService
        from cyborg_server.services.prompt_assembler import load_workspace_prompt, build_chat_messages
        from cyborg_server.services.workspace_tools import make_workspace_tools
        from cyborg_server.services.tools import Tool
        from cyborg_server.services.session_agenda_service import SessionAgendaService

        db = ctx.db

        # Find originating session and outreach metadata from route
        route = await db.fetch_one(
            "SELECT metadata FROM session_routes WHERE session_key = ?",
            (current_session_key,),
        )
        if not route or not route["metadata"]:
            return json.dumps({"ok": False, "error": "No active outreach to report"})

        meta = json.loads(route["metadata"])
        origin_session_key = meta.get("outreach_initiated_from")
        if not origin_session_key:
            return json.dumps({"ok": False, "error": "No active outreach to report"})

        objective = meta.get("outreach_objective", "unknown")
        requestor = meta.get("outreach_requestor", "unknown")

        # Look up target contact name for context
        target_contact = await db.fetch_one(
            "SELECT c.name FROM session_routes sr "
            "JOIN contacts c ON c.id = sr.contact_id AND c.deleted_at IS NULL "
            "WHERE sr.session_key = ?",
            (current_session_key,),
        )
        target_contact_name = target_contact["name"] if target_contact else "unknown"

        # Clear outreach metadata from route
        meta.pop("outreach_initiated_from", None)
        meta.pop("outreach_objective", None)
        meta.pop("outreach_requestor", None)
        meta.pop("outreach_message", None)
        await db.execute(
            "UPDATE session_routes SET metadata = ? WHERE session_key = ?",
            (json.dumps(meta) if meta else None, current_session_key),
        )

        # Build result content for source session
        origin_chat_id = _session_key_to_chat_id(origin_session_key)
        settings = ctx.settings

        result_content = (
            f"## Outreach Result\n"
            f"Contact: {target_contact_name}\n"
            f"Objective: {objective}\n"
            f"Requested by: {requestor}\n\n"
            f"{result}"
        )

        # Store result in source session's message history
        session_svc = SessionService(ctx)
        await session_svc.add_message(
            origin_session_key, "user", result_content,
            channel="whatsapp",
            metadata={"outreach_result": True, "source_session": current_session_key},
        )

        # Dispatch an LLM call in the source session to receive the result
        agenda_svc = SessionAgendaService(ctx)
        origin_agenda = await agenda_svc.get_effective_agenda(
            origin_session_key, "whatsapp",
        )

        workspace_prompt = load_workspace_prompt(settings.harness.workspace_dir)
        system_content = "\n\n".join(
            p for p in (workspace_prompt, origin_agenda) if p
        )

        messages = await build_chat_messages(
            result_content,
            origin_session_key,
            db=db,
            system_content=system_content,
            max_history=20,
        )

        # Build tools for source session
        origin_tools = make_workspace_tools(ctx, session_key=origin_session_key)

        message_was_sent = [False]

        async def _send_reply(text: str) -> str:
            message_was_sent[0] = True
            if text.strip().upper() == "NO_REPLY":
                return "No reply sent."
            if not origin_chat_id:
                return "Error: cannot resolve chat for source session"
            if not wa_service.connected:
                return "Error: WhatsApp bridge not connected"
            await wa_service.send_message(origin_chat_id, text)
            return "Message sent"

        origin_tools.append(Tool(
            name="send_whatsapp_message",
            description=(
                "Send a reply to the current WhatsApp conversation. "
                "You MUST call this tool to deliver your response — your text output will NOT be sent."
            ),
            parameters={"text": {"type": "string", "description": "The message text to send."}},
            required=["text"],
            handler=_send_reply,
        ))

        dispatch_id = str(uuid4())

        async def _run_dispatch() -> str:
            llm_result = await LLMDispatchService(ctx).chat_with_tools(
                messages, origin_tools,
                call_category="outreach_result",
                session_key=origin_session_key,
                dispatch_id=dispatch_id,
            )

            # Tap: if LLM didn't use send_whatsapp_message, give it a second chance.
            if not message_was_sent[0] and llm_result.strip():
                from cyborg_server.services.tap import tap_dispatch
                llm_result = await tap_dispatch(
                    ctx, messages=messages, tools=origin_tools,
                    session_key=origin_session_key,
                    send_tool_name="send_whatsapp_message",
                    first_result=llm_result,
                    call_category="outreach_result",
                    dispatch_id=dispatch_id,
                )

            # Record in source session history
            await session_svc.add_message(
                origin_session_key, "assistant", llm_result,
                channel="whatsapp",
            )

            return llm_result

        asyncio.create_task(_run_dispatch())

        logger.info(
            "Outreach finished from %s to %s, dispatching result to source session",
            current_session_key, origin_session_key,
        )

        return json.dumps({
            "ok": True,
            "dispatched_to": origin_session_key,
        })

    return [finish_outreach]
