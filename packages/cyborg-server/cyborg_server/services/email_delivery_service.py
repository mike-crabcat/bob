"""Outbound email delivery via AgentMail."""

from __future__ import annotations

import logging
from typing import Any

from cyborg_server.config import Settings
from cyborg_server.database import Database
from cyborg_server.services.agentmail_client import AgentMailClient
from cyborg_server.services.base import BaseService


logger = logging.getLogger(__name__)


class EmailDeliveryService(BaseService):
    """Send outgoing email via AgentMail."""

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

        Finds the latest message in the thread and replies to it.
        Falls back to sending a new threaded message if no existing message found.
        """
        # Look up the latest message in this thread to get its agentmail_message_id
        latest = await self.db.fetch_one(
            """
            SELECT em.agentmail_message_id, em.inbox_id
            FROM email_messages em
            WHERE em.thread_id = ?
            ORDER BY em.message_timestamp DESC
            LIMIT 1
            """,
            (thread_id,),
        )

        inbox = await self.db.fetch_one(
            "SELECT agentmail_inbox_id FROM email_inboxes WHERE id = ? AND deleted_at IS NULL",
            (inbox_id,),
        )
        if inbox is None:
            raise ValueError(f"Inbox {inbox_id} not found")

        agentmail_inbox_id = inbox["agentmail_inbox_id"]

        if latest is not None and latest["agentmail_message_id"]:
            try:
                return await self.client.reply_message(
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

        # Fallback: send as new message with thread_id
        return await self.client.send_message(
            agentmail_inbox_id,
            to="",  # thread_id handles routing
            subject="",
            text=text,
            html=html,
            thread_id=thread_id,
        )

    async def send_new_email(
        self,
        *,
        inbox_id: str,
        to: str,
        subject: str,
        text: str,
        html: str | None = None,
        cc: list[str] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Send a new email from a registered inbox."""
        inbox = await self.db.fetch_one(
            "SELECT agentmail_inbox_id FROM email_inboxes WHERE id = ? AND deleted_at IS NULL",
            (inbox_id,),
        )
        if inbox is None:
            raise ValueError(f"Inbox {inbox_id} not found")

        return await self.client.send_message(
            inbox["agentmail_inbox_id"],
            to=to,
            subject=subject,
            text=text,
            html=html,
            cc=cc,
            attachments=attachments,
        )
