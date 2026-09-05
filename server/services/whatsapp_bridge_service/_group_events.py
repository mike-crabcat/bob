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

from server.services.base import utcnow
from server.services.dispatch_runner import is_no_reply
from server.services.whatsapp_bridge_service._media import _jid_to_phone


logger = logging.getLogger(__name__)


class GroupEventsMixin:

    def _contacts(self):
        from server.repositories.contacts import ContactRepository
        return ContactRepository(self.db)
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
        from server.repositories.groups import GroupRepository
        groups = GroupRepository(self.db)
        group_id = await groups.upsert_group(
            group_jid, name=group_name, description=description,
            member_count=len(participants), now_iso=now_iso)

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
            await groups.upsert_member(
                group_id, contact_id, display_name=display_name, now_iso=now_iso,
                is_admin=is_admin, is_super_admin=is_super_admin)

        # Mark departed members
        await groups.mark_departed_except(group_id, seen_contact_ids, now_iso)

        # Upsert all participants
        agent_id = "main"
        key_part = group_jid.split("@")[0] if "@" in group_jid else group_jid
        session_key = f"agent:{agent_id}:whatsapp:group:{key_part}"

        from server.repositories.participants import ParticipantRepository
        participants_repo = ParticipantRepository(self.db)
        for p in participants:
            p_jid = p.get("jid", "")
            phone_number = _jid_to_phone(p_jid)
            display_name = p.get("display_name", "")
            contact = await self._contacts().get_by_phone(phone_number)
            if not contact:
                continue
            await participants_repo.upsert(
                session_key, phone_number,
                # Roster syncs often carry no per-member name; fall back to
                # the contact's name, and pass '' (upsert then keeps any
                # previously learned pushname) — never the contact UUID,
                # which used to clobber real names on every restart's
                # connect-time sync.
                display_name=display_name or contact.get("name") or "",
                contact_id=contact["id"],
                is_trusted=bool(contact.get("is_trusted")), now_iso=now_iso)
        from server.repositories.conversations import ConversationRepository
        await ConversationRepository(self.db).register_endpoint(
            session_key, endpoint_kind="group", address=group_jid)

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
        from server.repositories.groups import GroupRepository
        groups = GroupRepository(self.db)
        group_id = await groups.ensure_group(group_jid, group_name, now_iso)

        agent_id = "main"
        key_part = group_jid.split("@")[0] if "@" in group_jid else group_jid
        session_key = f"agent:{agent_id}:whatsapp:group:{key_part}"

        join_names: list[str] = []
        for jid in joined_jids:
            phone_number = _jid_to_phone(jid)
            # Try to get a display name from existing participants or contacts
            display_name = ""
            existing = await self._contacts().get_by_phone(phone_number)
            if existing:
                display_name = existing["name"]

            contact_id, _ = await self._resolve_or_seed_contact(phone_number, display_name)
            join_names.append(display_name or phone_number)

            # Upsert group member (re-join if previously left)
            await groups.upsert_member(
                group_id, contact_id, display_name=display_name, now_iso=now_iso)

            # Upsert session participant
            contact = await self._contacts().get_by_phone(phone_number)
            if contact:
                from server.repositories.participants import ParticipantRepository
                await ParticipantRepository(self.db).upsert(
                    session_key, phone_number,
                    display_name=display_name or phone_number,
                    contact_id=contact["id"],
                    is_trusted=bool(contact.get("is_trusted")), now_iso=now_iso)

        leave_names: list[str] = []
        for jid in left_jids:
            phone_number = _jid_to_phone(jid)
            existing_contact = await self._contacts().get_by_phone(phone_number)
            if existing_contact:
                leave_names.append(existing_contact["name"] or phone_number)
                await groups.mark_left(group_id, existing_contact["id"], now_iso)
            else:
                leave_names.append(phone_number)

        # Update member count
        member_count = await groups.refresh_member_count(group_id, now_iso)

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
            sender_contact = await self._contacts().get_by_phone(sender_phone)
            if sender_contact:
                sender_name = sender_contact["name"]

        # Ensure the endpoint binding exists
        from server.repositories.conversations import ConversationRepository
        await ConversationRepository(self.db).register_endpoint(
            session_key, endpoint_kind="group", address=group_jid)

        # Store notification as user message and dispatch
        settings = self._get_settings()
        if not settings.openai.enabled:
            return

        # Determine trust from the binding
        from server.repositories.conversations import ConversationRepository
        route = await ConversationRepository(self.db).route_for(session_key)
        is_trusted = False
        if route and route["contact_id"]:
            trusted = await self._contacts().is_trusted(route["contact_id"])
            is_trusted = bool(trusted)

        from server.services.session_service import SessionService
        session_svc = SessionService(self.ctx)
        # Stored with provenance + a self-describing frame so later replays
        # show WHAT happened without the reply guidance (that is turn-scoped,
        # in system_content below — it must not pollute history forever).
        notification_row = (
            "## Group Member Change\n"
            f"Group: {group_name or group_jid}\n"
            f"Changed by: {sender_name or sender_jid or 'unknown'}\n\n"
            f"{notification_text}"
        )
        await session_svc.add_message(
            session_key, "user", notification_row,
            channel="whatsapp", sender_id=None, dispatched=0,
            provenance="group_event",
        )

        # Build system prompt
        from server.services.session_agenda_service import SessionAgendaService
        from server.services.prompt_assembler import load_workspace_prompt

        agenda_svc = SessionAgendaService(self.ctx)
        agenda = await agenda_svc.get_effective_agenda(
            session_key, "whatsapp",
            contact_id=route["contact_id"] if route else None, is_trusted=is_trusted,
        )
        workspace_prompt = await load_workspace_prompt(settings.harness.workspace_dir, db=self.db)
        from server.services.context_assembler import ContextAssembler
        participants_prompt = await ContextAssembler(self.ctx).participants_prompt(session_key)

        # Turn-scoped handling note: greets are the point of this trigger, but
        # irrelevant changes may stay silent. Previously injected by overriding
        # the last user message — which could clobber a human message and is
        # superseded by the [Group event] marker + trailing presentation.
        handling_note = (
            "## Group Member Change Handling\n"
            "This turn was triggered by a group membership change (marked "
            "[Group event] in the conversation). You do not need to reply unless "
            "the change is contextually relevant — greeting a new member, "
            "acknowledging a key person leaving. If no response is needed, call "
            "send_whatsapp_message with 'NO_REPLY'."
        )
        system_content = "\n\n".join(
            p for p in (workspace_prompt, participants_prompt, agenda, handling_note) if p
        )

        # Build tools
        from server.services.llm_dispatch import LLMDispatchService
        from server.services.tools import Tool
        from server.services.tool_registry import build_common_tools
        from server.services.group_tools import make_group_tools

        tools = build_common_tools(self.ctx, session_key=session_key, is_trusted=is_trusted, contact_id=route["contact_id"] if route else None)
        tools.extend(make_group_tools(self.ctx, session_key=session_key))

        wa_service = self
        chat_id = group_jid
        message_was_sent = [False]
        sent_texts: list[str] = []
        send_seq = [0]

        async def _send_whatsapp_message(text: str, media_path: str = "") -> str:
            from server.services.effects import emit_and_deliver

            message_was_sent[0] = True
            if is_no_reply(text):
                return "No reply sent."
            seq = send_seq[0]
            send_seq[0] += 1
            if media_path:
                from server.services.whatsapp_bridge_service._media import (
                    _prepare_media,
                    resolve_sendable_media,
                )

                resolved = resolve_sendable_media(
                    settings.harness.workspace_dir, media_path)
                if isinstance(resolved, str):
                    return resolved
                prepared = await _prepare_media(str(resolved))
                if prepared is None:
                    return "Error: failed to prepare media for sending"
                result = await emit_and_deliver(
                    self.ctx, kind="whatsapp_send_media",
                    idempotency_key=f"whatsapp_send_media:{dispatch_id}:{seq}",
                    payload={"chat_id": chat_id, "file_path": prepared, "caption": text})
                if not result.get("ok"):
                    return f"Error sending media: {result.get('error', 'delivery failed')}"
                sent_texts.append(f"[Image: {text}]" if text else f"[Image: {resolved.name}]")
                return f"Media sent (request_id={result.get('external_result_id')})"
            result = await emit_and_deliver(
                self.ctx, kind="whatsapp_send",
                idempotency_key=f"whatsapp_send:{dispatch_id}:{seq}",
                payload={"chat_id": chat_id, "text": text})
            if not result.get("ok"):
                return f"Error sending message: {result.get('error', 'delivery failed')}"
            sent_texts.append(text)
            return f"Message sent (request_id={result.get('external_result_id')})"

        tools.append(Tool(
            name="send_whatsapp_message",
            description=(
                "Send a reply to the current WhatsApp conversation. "
                "You MUST call this tool to deliver your response — your text output will NOT be sent. "
                "Optionally attach an image or media file by providing media_path."
            ),
            parameters={
                "text": {"type": "string", "description": "The message text to send (used as caption when media_path is provided)."},
                "media_path": {"type": "string", "description": "Optional path to an image or media file, relative to the workspace directory."},
            },
            required=["text"],
            handler=_send_whatsapp_message,
        ))

        dispatch_id = str(uuid4())

        from server.services.dispatch_runner import DispatchRunner, DispatchSpec

        dispatch_spec = DispatchSpec(
            session_key=session_key,
            system_content=system_content,
            tools=tools,
            call_category="whatsapp_group_member_change",
            send_tool_name="send_whatsapp_message",
            dispatch_id=dispatch_id,
            contact_id=route["contact_id"] if route else None,
            channel="whatsapp",
            max_history=100,
            history_policy="merged_skip_no_reply",
            message_was_sent=message_was_sent,
            sent_texts=sent_texts,
        )

        asyncio.create_task(DispatchRunner(self.ctx).run(dispatch_spec))

