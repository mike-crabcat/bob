"""Email inbox polling and incoming message processing."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from cyborg_server.config import Settings
from cyborg_server.database import Database
from cyborg_server.models import SessionRouteCreate, SessionRouteKind
from cyborg_server.services.agentmail_client import AgentMailClient
from cyborg_server.services.base import BaseService, json_dumps, utcnow
from cyborg_server.services.session_route_service import SessionRouteService


logger = logging.getLogger(__name__)


class EmailPollingService(BaseService):
    """Poll AgentMail inboxes for new messages and dispatch to OpenClaw."""

    def __init__(
        self,
        db: Database,
        *,
        agentmail_client: AgentMailClient | None = None,
    ) -> None:
        super().__init__(db)
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

    def _get_settings(self) -> Settings:
        current = getattr(self.db, "settings", None)
        if isinstance(current, Settings):
            return current
        return Settings.from_env()

    async def poll_all_inboxes(self) -> int:
        """Poll all active email inboxes for new messages.

        Only polls inboxes where last_polled_at is older than poll_interval_seconds.
        Returns total new messages processed.
        """
        settings = self._get_settings()
        if not settings.agentmail.enabled:
            return 0

        inboxes = await self.db.fetch_all(
            "SELECT * FROM email_inboxes WHERE deleted_at IS NULL AND is_active = 1"
        )
        if not inboxes:
            return 0

        total = 0
        for inbox in inboxes:
            if not self._should_poll(inbox, settings.agentmail.poll_interval_seconds):
                continue
            try:
                total += await self.poll_inbox(inbox)
            except Exception:
                logger.exception("Failed to poll inbox %s", inbox["id"])
        return total

    async def poll_inbox(self, inbox: dict[str, Any] | str) -> int:
        """Poll a single inbox for unread messages.

        Args:
            inbox: Database row dict or agentmail_inbox_id string.
        """
        if isinstance(inbox, str):
            row = await self.db.fetch_one(
                "SELECT * FROM email_inboxes WHERE agentmail_inbox_id = ? AND deleted_at IS NULL",
                (inbox,),
            )
            if row is None:
                return 0
            inbox = row

        inbox_id = inbox["id"]
        agentmail_inbox_id = inbox["agentmail_inbox_id"]

        now = utcnow()
        messages_data = await self.client.list_messages(
            agentmail_inbox_id,
            limit=50,
            labels=["unread"],
        )

        messages = messages_data.get("messages", []) if isinstance(messages_data, dict) else []
        count = 0
        for message in messages:
            try:
                processed = await self.process_incoming_message(inbox, message)
                if processed:
                    count += 1
            except Exception:
                logger.exception(
                    "Failed to process message %s in inbox %s",
                    message.get("id", "?"), inbox_id,
                )

        # Update last_polled_at
        await self.db.execute(
            "UPDATE email_inboxes SET last_polled_at = ?, updated_at = ? WHERE id = ?",
            (now.isoformat(), now.isoformat(), inbox_id),
        )
        return count

    async def process_incoming_message(
        self,
        inbox: dict[str, Any],
        message: dict[str, Any],
    ) -> bool:
        """Process a single incoming email message.

        Returns True if the message was newly processed, False if already seen.
        """
        agentmail_message_id = message.get("id", "")
        if not agentmail_message_id:
            logger.warning("Skipping message with no ID in inbox %s", inbox["id"])
            return False

        # Dedup check
        existing = await self.db.fetch_one(
            "SELECT id FROM email_messages WHERE agentmail_message_id = ?",
            (agentmail_message_id,),
        )
        if existing is not None:
            return False

        thread_id = message.get("thread_id", agentmail_message_id)
        now = utcnow()

        # Store the message
        message_id = str(uuid4())
        await self.db.execute(
            """
            INSERT INTO email_messages (
                id, inbox_id, agentmail_message_id, thread_id,
                subject, sender_email, sender_name,
                to_addresses, cc_addresses,
                text_body, html_body, preview, labels,
                has_attachments, in_reply_to,
                message_timestamp, processed_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                inbox["id"],
                agentmail_message_id,
                thread_id,
                message.get("subject"),
                (message.get("from", {}) if isinstance(message.get("from"), dict) else {}).get("email", "")
                    if isinstance(message.get("from"), dict) else str(message.get("from", "")),
                (message.get("from", {}) if isinstance(message.get("from"), dict) else {}).get("name", "")
                    if isinstance(message.get("from"), dict) else None,
                json_dumps(message.get("to", [])),
                json_dumps(message.get("cc", [])),
                message.get("extracted_text") or message.get("text", ""),
                message.get("extracted_html") or message.get("html"),
                message.get("preview"),
                json_dumps(message.get("labels", [])),
                1 if message.get("attachments") else 0,
                message.get("in_reply_to"),
                message.get("created_at", now.isoformat()),
                now.isoformat(),
                now.isoformat(),
            ),
        )

        # Resolve or create the thread record
        thread = await self._resolve_or_create_thread(inbox, message, thread_id, now)

        # Mark message read in AgentMail
        try:
            await self.client.update_message(
                inbox["agentmail_inbox_id"],
                agentmail_message_id,
                remove_labels=["unread"],
            )
        except Exception:
            logger.warning(
                "Failed to mark message %s as read in AgentMail",
                agentmail_message_id,
                exc_info=True,
            )

        # Update thread message count
        await self.db.execute(
            """
            UPDATE email_threads
            SET message_count = message_count + 1, last_message_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now.isoformat(), now.isoformat(), thread["id"]),
        )

        # Dispatch to OpenClaw
        await self._dispatch_to_openclaw(thread, message, inbox)
        return True

    async def _resolve_or_create_thread(
        self,
        inbox: dict[str, Any],
        message: dict[str, Any],
        thread_id: str,
        now: Any,
    ) -> dict[str, Any]:
        """Find or create an email_threads record for this message."""
        existing = await self.db.fetch_one(
            """
            SELECT * FROM email_threads
            WHERE inbox_id = ? AND agentmail_thread_id = ? AND deleted_at IS NULL
            """,
            (inbox["id"], thread_id),
        )
        if existing is not None:
            return existing

        # New thread — create session route and thread record
        session_key = self._build_session_key(thread_id)
        settings = self._get_settings()

        # Try to match sender to existing contact
        sender_email = message.get("from", {})
        if isinstance(sender_email, dict):
            sender_email = sender_email.get("email", "")
        else:
            sender_email = str(sender_email)

        contact_id = None
        if sender_email:
            contact = await self.db.fetch_one(
                "SELECT id FROM contacts WHERE email = ? AND deleted_at IS NULL LIMIT 1",
                (sender_email,),
            )
            if contact:
                contact_id = contact["id"]

        # Create session route
        route_service = SessionRouteService(self.db)
        await route_service.create_route(SessionRouteCreate(
            channel="email",
            session_key=session_key,
            kind=SessionRouteKind.THREAD,
            chat_id=thread_id,
            metadata={
                "inbox_id": inbox["id"],
                "agentmail_inbox_id": inbox["agentmail_inbox_id"],
            },
        ))

        # Create thread record
        thread_record_id = str(uuid4())
        now_iso = now.isoformat()
        await self.db.execute(
            """
            INSERT INTO email_threads (
                id, inbox_id, agentmail_thread_id, subject,
                contact_id, session_key,
                message_count, last_message_at, is_active,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, 1, ?, ?)
            """,
            (
                thread_record_id,
                inbox["id"],
                thread_id,
                message.get("subject"),
                contact_id,
                session_key,
                now_iso,
                now_iso,
                now_iso,
            ),
        )
        return await self.db.fetch_one(
            "SELECT * FROM email_threads WHERE id = ?",
            (thread_record_id,),
        )

    async def _dispatch_to_openclaw(
        self,
        thread: dict[str, Any],
        message: dict[str, Any],
        inbox: dict[str, Any],
    ) -> None:
        """Dispatch an incoming email to OpenClaw via the gateway."""
        settings = self._get_settings()
        if not settings.openclaw.enabled:
            logger.info("OpenClaw not configured, skipping dispatch for email thread %s", thread["id"])
            return

        from cyborg_server.services.openclaw_hook_service import OpenClawHookService

        sender = message.get("from", {})
        if isinstance(sender, dict):
            sender_name = sender.get("name", sender.get("email", "Unknown"))
            sender_email = sender.get("email", "unknown")
        else:
            sender_name = str(sender)
            sender_email = str(sender)

        subject = message.get("subject", "(no subject)")
        body = message.get("extracted_text") or message.get("text", "")

        prompt = "\n".join([
            "Incoming email message received.",
            "",
            f"From: {sender_name} <{sender_email}>",
            f"Subject: {subject}",
            f"Thread ID: {thread['agentmail_thread_id']}",
            f"Inbox: {inbox['email_address']}",
            "",
            "## Email Body",
            body,
            "",
            "## Instructions",
            "This email arrived in a monitored inbox. Review the content and decide how to respond.",
            f"Use `cyborg email reply --inbox {inbox['id']} --message-id {message.get('id', '')} --text \"<your reply>\"` to respond.",
            "Keep your reply professional and concise.",
        ])

        hook_service = OpenClawHookService(
            self.db,
            cyborg_service_url=settings.resolved_public_url,
        )
        await hook_service._send_gateway_request(
            "agent",
            {
                "message": prompt,
                "deliver": False,
                "sessionKey": thread["session_key"],
                "thinking": "high",
                "timeout": int(settings.openclaw.timeout_seconds),
            },
        )

    def _build_session_key(self, thread_id: str) -> str:
        settings = self._get_settings()
        agent_id = settings.openclaw.agent_id or "main"
        return f"agent:{agent_id}:email:thread:{thread_id}"

    def _should_poll(self, inbox: dict[str, Any], poll_interval: float) -> bool:
        """Check if enough time has elapsed since the last poll for this inbox."""
        last_polled = inbox.get("last_polled_at")
        if not last_polled:
            return True
        try:
            from datetime import datetime
            last = datetime.fromisoformat(last_polled)
            elapsed = (utcnow() - last).total_seconds()
            return elapsed >= poll_interval
        except (ValueError, TypeError):
            return True
