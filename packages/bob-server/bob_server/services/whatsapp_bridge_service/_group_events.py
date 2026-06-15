"""Group sync handlers extracted from WhatsAppBridgeService.

Mixin: these methods rely on the host class providing ``self.ctx``,
``self._db``, ``self.send_message``, ``self._resolve_or_seed_contact``, etc.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from uuid import uuid4

from bob_server.models import SessionRouteCreate, SessionRouteKind
from bob_server.services.base import utcnow
from bob_server.services.session_route_service import SessionRouteService
from bob_server.services.whatsapp_bridge_service._media import _jid_to_phone


logger = logging.getLogger(__name__)


class GroupEventsMixin:
    """Group membership and metadata sync handlers."""

    async def _handle_group_sync(self, payload: dict[str, Any]) -> None:
        """Handle full group participant sync from bridge (fires on connect for each group)."""
        group_jid = payload.get("group_jid", "")
        group_name = payload.get("group_name", "")
        description = payload.get("description", "")
        participants = payload.get("participants", [])

        if not group_jid:
            return

        logger.info("group sync: %s (%s) with %d participants", group_name, group_jid, len(participants))

        now_iso = utcnow().isoformat()

        # Upsert group
        existing_group = await self.db.fetch_one(
            "SELECT id FROM whatsappgroups WHERE whatsapp_jid = ? AND deleted_at IS NULL",
            (group_jid,),
        )
        if existing_group:
            group_id = existing_group["id"]
            await self.db.execute(
                "UPDATE whatsappgroups SET name = ?, description = ?, member_count = ?, updated_at = ? WHERE id = ?",
                (group_name, description, len(participants), now_iso, group_id),
            )
        else:
            group_id = str(uuid4())
            await self.db.execute(
                """INSERT INTO whatsappgroups (id, whatsapp_jid, name, description, member_count, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (group_id, group_jid, group_name, description, len(participants), now_iso, now_iso),
            )

        # Process each participant
        seen_contact_ids: set[str] = set()
        for p in participants:
            p_jid = p.get("jid", "")
            phone_number = _jid_to_phone(p_jid)
            display_name = p.get("display_name", "")
            is_admin = 1 if p.get("is_admin") else 0
            is_super_admin = 1 if p.get("is_super_admin") else 0

            contact_id, _ = await self._resolve_or_seed_contact(phone_number, display_name)
            seen_contact_ids.add(contact_id)

            # Upsert group member
            await self.db.execute(
                """INSERT INTO whatsappgroup_members (id, group_id, contact_id, is_admin, is_super_admin, display_name, joined_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(group_id, contact_id) DO UPDATE SET
                       is_admin = excluded.is_admin,
                       is_super_admin = excluded.is_super_admin,
                       display_name = excluded.display_name,
                       left_at = NULL,
                       updated_at = excluded.updated_at""",
                (str(uuid4()), group_id, contact_id, is_admin, is_super_admin, display_name, now_iso, now_iso, now_iso),
            )

        # Mark departed members
        if seen_contact_ids:
            placeholders = ",".join("?" for _ in seen_contact_ids)
            await self.db.execute(
                f"UPDATE whatsappgroup_members SET left_at = ?, updated_at = ? WHERE group_id = ? AND left_at IS NULL AND contact_id NOT IN ({placeholders})",
                (now_iso, now_iso, group_id, *seen_contact_ids),
            )

        # Upsert all participants into session_participants
        agent_id = "main"
        key_part = group_jid.split("@")[0] if "@" in group_jid else group_jid
        session_key = f"agent:{agent_id}:whatsapp:group:{key_part}"

        for p in participants:
            p_jid = p.get("jid", "")
            phone_number = _jid_to_phone(p_jid)
            display_name = p.get("display_name", "")
            contact = await self.db.fetch_one(
                "SELECT id, is_trusted FROM contacts WHERE phone_number = ? AND deleted_at IS NULL LIMIT 1",
                (phone_number,),
            )
            if not contact:
                continue
            await self.db.execute(
                """INSERT INTO session_participants (session_key, identifier, display_name, contact_id, is_trusted, last_active_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(session_key, identifier) DO UPDATE SET
                       display_name = excluded.display_name,
                       contact_id = COALESCE(excluded.contact_id, session_participants.contact_id),
                       is_trusted = CASE WHEN excluded.contact_id IS NOT NULL THEN excluded.is_trusted ELSE session_participants.is_trusted END,
                       last_active_at = excluded.last_active_at""",
                (session_key, phone_number, display_name or contact["id"], contact["id"],
                 1 if contact.get("is_trusted") else 0, now_iso),
            )

        # Ensure session route exists
        route_service = SessionRouteService(self.ctx)
        from bob_server.exceptions import ConflictError
        try:
            await route_service.create_route(SessionRouteCreate(
                channel="whatsapp",
                session_key=session_key,
                kind=SessionRouteKind.GROUP,
                chat_id=group_jid,
            ))
        except ConflictError:
            pass

    async def _handle_group_member_change(self, payload: dict[str, Any]) -> None:
        """Handle incremental group member join/leave events."""
        group_jid = payload.get("group_jid", "")
        group_name = payload.get("group_name", "")
        sender_jid = payload.get("sender_jid", "")
        joined_jids = payload.get("joined_jids", [])
        left_jids = payload.get("left_jids", [])

        if not group_jid or (not joined_jids and not left_jids):
            return

        logger.info("group member change: %s joined=%d left=%d", group_jid, len(joined_jids), len(left_jids))

        now_iso = utcnow().isoformat()

        # Resolve or create group
        group = await self.db.fetch_one(
            "SELECT id, name FROM whatsappgroups WHERE whatsapp_jid = ? AND deleted_at IS NULL",
            (group_jid,),
        )
        if not group:
            group_id = str(uuid4())
            await self.db.execute(
                """INSERT INTO whatsappgroups (id, whatsapp_jid, name, member_count, created_at, updated_at)
                   VALUES (?, ?, ?, 0, ?, ?)""",
                (group_id, group_jid, group_name, now_iso, now_iso),
            )
        else:
            group_id = group["id"]

        agent_id = "main"
        key_part = group_jid.split("@")[0] if "@" in group_jid else group_jid
        session_key = f"agent:{agent_id}:whatsapp:group:{key_part}"

        join_names: list[str] = []
        for jid in joined_jids:
            phone_number = _jid_to_phone(jid)
            # Try to get a display name from existing session_participants or contacts
            display_name = ""
            existing = await self.db.fetch_one(
                "SELECT name FROM contacts WHERE phone_number = ? AND deleted_at IS NULL LIMIT 1",
                (phone_number,),
            )
            if existing:
                display_name = existing["name"]

            contact_id, _ = await self._resolve_or_seed_contact(phone_number, display_name)
            join_names.append(display_name or phone_number)

            # Upsert group member (re-join if previously left)
            await self.db.execute(
                """INSERT INTO whatsappgroup_members (id, group_id, contact_id, display_name, joined_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(group_id, contact_id) DO UPDATE SET
                       left_at = NULL,
                       joined_at = excluded.joined_at,
                       display_name = COALESCE(excluded.display_name, whatsappgroup_members.display_name),
                       updated_at = excluded.updated_at""",
                (str(uuid4()), group_id, contact_id, display_name, now_iso, now_iso, now_iso),
            )

            # Upsert session participant
            contact = await self.db.fetch_one(
                "SELECT id, is_trusted FROM contacts WHERE phone_number = ? AND deleted_at IS NULL LIMIT 1",
                (phone_number,),
            )
            if contact:
                await self.db.execute(
                    """INSERT INTO session_participants (session_key, identifier, display_name, contact_id, is_trusted, last_active_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(session_key, identifier) DO UPDATE SET
                           display_name = COALESCE(excluded.display_name, session_participants.display_name),
                           contact_id = COALESCE(excluded.contact_id, session_participants.contact_id),
                           last_active_at = excluded.last_active_at""",
                    (session_key, phone_number, display_name or phone_number, contact["id"],
                     1 if contact.get("is_trusted") else 0, now_iso),
                )

        leave_names: list[str] = []
        for jid in left_jids:
            phone_number = _jid_to_phone(jid)
            existing_contact = await self.db.fetch_one(
                "SELECT id, name FROM contacts WHERE phone_number = ? AND deleted_at IS NULL LIMIT 1",
                (phone_number,),
            )
            if existing_contact:
                leave_names.append(existing_contact["name"] or phone_number)
                await self.db.execute(
                    "UPDATE whatsappgroup_members SET left_at = ?, updated_at = ? WHERE group_id = ? AND contact_id = ? AND left_at IS NULL",
                    (now_iso, now_iso, group_id, existing_contact["id"]),
                )
            else:
                leave_names.append(phone_number)

        # Update member count
        count_row = await self.db.fetch_one(
            "SELECT COUNT(*) as cnt FROM whatsappgroup_members WHERE group_id = ? AND left_at IS NULL",
            (group_id,),
        )
        member_count = count_row["cnt"] if count_row else 0
        await self.db.execute(
            "UPDATE whatsappgroups SET member_count = ?, updated_at = ? WHERE id = ?",
            (member_count, now_iso, group_id),
        )

        # Build notification text
        notification_parts = []
        if join_names:
            notification_parts.append(f"Members joined: {', '.join(join_names)}")
        if leave_names:
            notification_parts.append(f"Members left: {', '.join(leave_names)}")
        notification_text = ". ".join(notification_parts)

        # Resolve sender name
        sender_name = ""
        if sender_jid:
            sender_phone = _jid_to_phone(sender_jid)
            sender_contact = await self.db.fetch_one(
                "SELECT name FROM contacts WHERE phone_number = ? AND deleted_at IS NULL LIMIT 1",
                (sender_phone,),
            )
            if sender_contact:
                sender_name = sender_contact["name"]

        # Ensure session route exists
        route_service = SessionRouteService(self.ctx)
        from bob_server.exceptions import ConflictError
        try:
            await route_service.create_route(SessionRouteCreate(
                channel="whatsapp",
                session_key=session_key,
                kind=SessionRouteKind.GROUP,
                chat_id=group_jid,
            ))
        except ConflictError:
            pass

        # Store notification as user message and dispatch
        settings = self._get_settings()
        if not settings.openai.enabled:
            return

        # Determine trust from session route
        route = await self.db.fetch_one(
            "SELECT contact_id FROM session_routes WHERE session_key = ?",
            (session_key,),
        )
        is_trusted = False
        if route and route["contact_id"]:
            contact = await self.db.fetch_one(
                "SELECT is_trusted FROM contacts WHERE id = ? AND deleted_at IS NULL",
                (route["contact_id"],),
            )
            if contact:
                is_trusted = bool(contact.get("is_trusted", 0))

        from bob_server.services.session_service import SessionService
        session_svc = SessionService(self.ctx)
        await session_svc.add_message(
            session_key, "user", notification_text,
            channel="whatsapp", sender_id=None, dispatched=0,
        )

        # Build system prompt
        from bob_server.services.session_agenda_service import SessionAgendaService
        from bob_server.services.prompt_assembler import load_workspace_prompt, build_chat_messages

        agenda_svc = SessionAgendaService(self.ctx)
        agenda = await agenda_svc.get_effective_agenda(
            session_key, "whatsapp",
            contact_id=route["contact_id"] if route else None, is_trusted=is_trusted,
        )
        workspace_prompt = await load_workspace_prompt(settings.harness.workspace_dir, db=self.db)
        participants_prompt = await self._build_participants_prompt(session_key)

        system_content = "\n\n".join(
            p for p in (workspace_prompt, participants_prompt) if p
        )

        # Build tools
        from bob_server.services.llm_dispatch import LLMDispatchService
        from bob_server.services.tools import Tool
        from bob_server.services.tool_registry import build_common_tools
        from bob_server.services.group_tools import make_group_tools

        tools = build_common_tools(self.ctx, session_key=session_key, is_trusted=is_trusted, contact_id=route["contact_id"] if route else None)
        tools.extend(make_group_tools(self.ctx, session_key=session_key))

        wa_service = self
        chat_id = group_jid
        message_was_sent = [False]
        sent_texts: list[str] = []
        sent_texts: list[str] = []

        async def _send_whatsapp_message(text: str) -> str:
            message_was_sent[0] = True
            if text.strip().upper() == "NO_REPLY":
                return "No reply sent."
            sent_texts.append(text)
            request_id = await wa_service.send_message(chat_id, text)
            return f"Message sent (request_id={request_id})"

        tools.append(Tool(
            name="send_whatsapp_message",
            description=(
                "Send a reply to the current WhatsApp conversation. "
                "You MUST call this tool to deliver your response — your text output will NOT be sent."
            ),
            parameters={"text": {"type": "string", "description": "The message text to send."}},
            required=["text"],
            handler=_send_whatsapp_message,
        ))

        dispatch_id = str(uuid4())

        user_content = "\n".join([
            "## Group Member Change Notification",
            f"Group: {group_name or group_jid}",
            f"Changed by: {sender_name or sender_jid or 'unknown'}",
            "",
            notification_text,
            "",
            "This is a system notification. You do not need to reply unless the member change "
            "is contextually relevant (e.g., greeting a new member, acknowledging a key person leaving). "
            "If no response is needed, call send_whatsapp_message with 'NO_REPLY'.",
        ])
        if agenda:
            user_content = agenda + "\n\n" + user_content

        async def _run_dispatch() -> str:
            from bob_server.services.session_dispatch_gate import SessionDispatchGate

            async with SessionDispatchGate.get_lock(session_key):
                claimed = await session_svc.mark_dispatched(session_key)
                if claimed == 0:
                    return ""

                messages = await build_chat_messages(
                    None, session_key,
                    db=self.db,
                    system_content=system_content,
                    max_history=100,
                )
                # Override the last user message with our notification
                if messages and messages[-1].get("role") == "user":
                    messages[-1]["content"] = user_content

                result = await LLMDispatchService(self.ctx).chat_with_tools(
                    messages, tools,
                    call_category="whatsapp_group_member_change",
                    session_key=session_key,
                    dispatch_id=dispatch_id,
                    contact_id=route["contact_id"] if route else None,
                )
                if not message_was_sent[0] and result.strip():
                    from bob_server.services.tap import tap_dispatch, tap_enabled
                    if tap_enabled():
                        result = await tap_dispatch(
                            self.ctx, messages=messages, tools=tools,
                            session_key=session_key,
                            send_tool_name="send_whatsapp_message",
                            first_result=result,
                            call_category="whatsapp_group_member_change",
                            dispatch_id=dispatch_id,
                            contact_id=route["contact_id"] if route else None,
                        )

                parts = [p for p in ([result] if result.strip() else []) + sent_texts if p.strip()]
                assistant_text = "\n\n".join(parts) if parts else result
                if not message_was_sent[0] and assistant_text.strip().upper().rstrip(".") in (
                    "NO_REPLY", "NO REPLY", "NOTHING TO SAY",
                ):
                    pass
                else:
                    await session_svc.add_message(session_key, "assistant", assistant_text, channel="whatsapp", dispatch_id=dispatch_id)

                return result

        asyncio.create_task(_run_dispatch())

