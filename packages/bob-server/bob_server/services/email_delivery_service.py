"""Outbound email delivery via AgentMail."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import uuid4

from bob_server.context import AppContext
from bob_server.database import Database
from bob_server.services.agentmail_client import AgentMailClient
from bob_server.services.base import BaseService, json_dumps, utcnow


logger = logging.getLogger(__name__)


class EmailDeliveryService(BaseService):
    """Send outgoing email via AgentMail."""

    def __init__(
        self,
        ctx: AppContext,
        *,
        agentmail_client: AgentMailClient | None = None,
    ) -> None:
        super().__init__(ctx)
        self._client = agentmail_client

    @property
    def client(self) -> AgentMailClient:
        if self._client is None:
            settings = self._get_settings()
            self._client = AgentMailClient(
                base_url=settings.agentmail.base_url,
                api_key=settings.agentmail.api_key,
            )
        return self._client

    async def send_reply(
        self,
        *,
        inbox_id: str,
        thread_id: str,
        text: str,
        html: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Send a reply in an existing email thread.

        thread_id is the agentmail_thread_id (not the local email_threads.id).
        Finds the latest message in the thread and replies to it.
        Falls back to sending a new threaded message if no existing message found.
        """
        # Look up the latest message in this thread to get its agentmail_message_id
        from bob_server.services.email_store import EmailStore
        store = EmailStore(self.db)
        latest = await store.latest_in_thread(thread_id)

        inbox = await store.get_inbox(inbox_id)
        if inbox is None:
            raise ValueError(f"Inbox {inbox_id} not found")

        agentmail_inbox_id = inbox["agentmail_inbox_id"]

        result: dict[str, Any] = {}
        if latest is not None and latest["agentmail_message_id"]:
            try:
                result = await self.client.reply_message(
                    agentmail_inbox_id,
                    latest["agentmail_message_id"],
                    text=text,
                    html=html,
                    reply_all=True,
                    attachments=attachments,
                )
            except Exception:
                logger.warning(
                    "Failed to reply to message %s, falling back to threaded send",
                    latest["agentmail_message_id"],
                    exc_info=True,
                )
                result = {}

        if not result:
            result = await self.client.send_message(
                agentmail_inbox_id,
                to="",  # thread_id handles routing
                subject="",
                text=text,
                html=html,
                thread_id=thread_id,
                attachments=attachments,
            )

        await self._persist_sent_message(
            inbox=inbox,
            agentmail_response=result,
            agentmail_thread_id=thread_id,
            text=text,
            html=html,
            has_attachments=bool(attachments),
        )
        return result

    async def send_new_email(
        self,
        *,
        inbox_id: str,
        to: str | list[str],
        subject: str,
        text: str,
        html: str | None = None,
        cc: list[str] | None = None,
        agenda: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        origin_session_key: str | None = None,
    ) -> dict[str, Any]:
        """Send a new email, create thread, persist message, and prime LLM context.

        Returns the AgentMail response dict.
        """
        from bob_server.services.email_store import EmailStore
        inbox = await EmailStore(self.db).get_inbox(inbox_id, active_only=True)
        if inbox is None:
            raise ValueError(f"Inbox {inbox_id} not found or inactive")

        result = await self.client.send_message(
            inbox["agentmail_inbox_id"],
            to=to,
            subject=subject,
            text=text,
            html=html,
            cc=cc,
            attachments=attachments,
        )

        agentmail_thread_id = result.get("thread_id", "")
        if not agentmail_thread_id:
            return result

        # Look up contact from recipient
        contact_id = None
        recipient_email = to if isinstance(to, str) else (to[0] if to else "")
        if recipient_email:
            from bob_server.repositories.contacts import ContactRepository
            contact = await ContactRepository(self.db).get_by_email(recipient_email)
            if contact:
                contact_id = contact["id"]

        # Create thread + session route
        from bob_server.services.email_polling_service import (
            CUSTOM_AGENDA_TEMPLATE,
            resolve_or_create_email_thread,
        )
        thread, is_new_thread = await resolve_or_create_email_thread(
            self.db,
            inbox=inbox,
            agentmail_thread_id=agentmail_thread_id,
            subject=subject,
            contact_id=contact_id,
            agenda=agenda,
            origin_session_key=origin_session_key,
        )

        # Persist agenda immediately (not waiting for lazy migration)
        if agenda and thread:
            from bob_server.services.session_agenda_service import SessionAgendaService
            await SessionAgendaService(self.ctx).set_agenda(thread["session_key"], agenda)

        # Persist outgoing message
        await self._persist_sent_message(
            inbox=inbox,
            agentmail_response=result,
            agentmail_thread_id=agentmail_thread_id,
            text=text,
            html=html,
            subject=subject,
            to_addresses=[to] if isinstance(to, str) else to,
            cc_addresses=cc,
            has_attachments=bool(attachments),
        )

        # Dispatch to LLM for context priming
        settings = self._get_settings()
        if settings.openai.enabled:
            from bob_server.services.llm_dispatch import LLMDispatchService
            from bob_server.services.session_service import SessionService
            from bob_server.services.prompt_assembler import load_workspace_prompt, local_now_prompt_line

            send_content = "\n".join([
                "## Email You Just Sent",
                "This is provided for your context. Do NOT reply — wait for the recipient to respond.",
                "",
                f"Subject: {subject}",
                f"To: {to}",
                "",
                text,
            ])

            session_key = thread["session_key"]
            # Canonicalize like inbound ingress (Bob3 Phase VI item 3): ensure
            # the conversation + binding exist for outbound-initiated threads
            # so they appear in conversation-centric views and merge correctly.
            try:
                from bob_server.repositories.conversations import ConversationRepository
                conversation = await ConversationRepository(self.db).ensure(
                    session_key, title=subject,
                    address=(session_key.rsplit(":", 1)[-1]
                             if ":email:thread:" in session_key else None),
                    endpoint_kind="thread")
                session_key = conversation["id"]
            except Exception:
                logger.warning("conversation ensure failed for %s", session_key, exc_info=True)
            logger.info("Dispatching send to LLM session=%s new_thread=%s", session_key, is_new_thread)

            workspace_prompt = await load_workspace_prompt(settings.harness.workspace_dir, db=self.db)
            custom_agenda = CUSTOM_AGENDA_TEMPLATE.format(agenda=agenda) if is_new_thread and agenda else None
            system_parts = [p for p in (
                workspace_prompt,
                custom_agenda,
                "You are managing an email conversation. The following is an outgoing email you sent for your context.",
            ) if p]
            # Stamp-only clock: this dispatch passes tools=[] (2026-09-01
            # time-grounding fan-out), so no get_time/bash hint.
            system_parts.append(local_now_prompt_line(tools_hint=False))

            messages = [
                {"role": "system", "content": "\n\n".join(system_parts)},
                {"role": "user", "content": send_content},
            ]

            dispatch_id = str(uuid4())
            ctx = self.ctx
            email_text = text

            async def _run_send_dispatch() -> str:
                result = await LLMDispatchService(ctx).chat_with_tools(
                    messages, [],
                    call_category="email_outgoing",
                    session_key=session_key,
                    dispatch_id=dispatch_id,
                )
                session_svc = SessionService(ctx)
                await session_svc.add_message(session_key, "assistant", email_text, channel="email", dispatch_id=dispatch_id)
                return result

            asyncio.create_task(_run_send_dispatch())
            logger.info("Send dispatch tracking for thread %s (dispatch=%s)", thread["id"], dispatch_id)

        return result

    async def _persist_sent_message(
        self,
        *,
        inbox: dict[str, Any],
        agentmail_response: dict[str, Any],
        agentmail_thread_id: str,
        text: str,
        html: str | None = None,
        subject: str | None = None,
        to_addresses: list[str] | None = None,
        cc_addresses: list[str] | None = None,
        has_attachments: bool = False,
    ) -> str:
        """Persist a sent message to email_messages and update thread stats."""
        agentmail_message_id = agentmail_response.get("message_id", "")
        if not agentmail_message_id:
            logger.warning("No message_id in AgentMail response, skipping persistence")
            return ""

        now = utcnow()
        message_id = str(uuid4())

        from bob_server.services.email_store import EmailStore
        store = EmailStore(self.db)
        await store.insert_message(
            message_id=message_id,
            inbox_id=inbox["id"],
            agentmail_message_id=agentmail_message_id,
            thread_id=agentmail_thread_id,
            subject=subject,
            sender_email=inbox["email_address"],
            sender_name=inbox.get("display_name"),
            to_addresses_json=json_dumps(to_addresses or []),
            cc_addresses_json=json_dumps(cc_addresses or []),
            text_body=text,
            html_body=html,
            preview=text[:200] if text else None,
            labels_json=json_dumps(["sent"]),
            has_attachments=has_attachments,
            in_reply_to=None,
            message_timestamp=now.isoformat(),
            now_iso=now.isoformat(),
        )

        # Update thread message count
        await store.bump_thread_stats_by_agentmail(agentmail_thread_id, now.isoformat())

        logger.info(
            "Persisted sent message %s in thread %s",
            message_id, agentmail_thread_id,
        )
        return message_id
