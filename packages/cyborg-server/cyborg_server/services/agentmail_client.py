"""Thin HTTP client for the AgentMail REST API."""

from __future__ import annotations

import logging
from typing import Any

import httpx


logger = logging.getLogger(__name__)


class AgentMailClient:
    """Async HTTP client wrapping the AgentMail REST API."""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 30.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(timeout),
        )

    async def list_messages(
        self,
        inbox_id: str,
        *,
        limit: int = 25,
        page_token: str | None = None,
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if page_token:
            params["page_token"] = page_token
        if labels:
            params["labels"] = ",".join(labels)
        response = await self._client.get(f"/v0/inboxes/{inbox_id}/messages", params=params)
        response.raise_for_status()
        return response.json()

    async def get_message(self, inbox_id: str, message_id: str) -> dict[str, Any]:
        response = await self._client.get(f"/v0/inboxes/{inbox_id}/messages/{message_id}")
        response.raise_for_status()
        return response.json()

    async def send_message(
        self,
        inbox_id: str,
        *,
        to: str | list[str],
        subject: str,
        text: str,
        html: str | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "to": to if isinstance(to, list) else [to],
            "subject": subject,
            "text": text,
        }
        if html:
            body["html"] = html
        if cc:
            body["cc"] = cc
        if bcc:
            body["bcc"] = bcc
        if thread_id:
            body["thread_id"] = thread_id
        response = await self._client.post(f"/v0/inboxes/{inbox_id}/messages", json=body)
        response.raise_for_status()
        return response.json()

    async def reply_message(
        self,
        inbox_id: str,
        message_id: str,
        *,
        text: str,
        html: str | None = None,
        reply_all: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"text": text}
        if html:
            body["html"] = html
        if reply_all:
            body["reply_all"] = True
        response = await self._client.post(
            f"/v0/inboxes/{inbox_id}/messages/{message_id}/reply",
            json=body,
        )
        response.raise_for_status()
        return response.json()

    async def update_message(
        self,
        inbox_id: str,
        message_id: str,
        *,
        add_labels: list[str] | None = None,
        remove_labels: list[str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if add_labels:
            body["add_labels"] = add_labels
        if remove_labels:
            body["remove_labels"] = remove_labels
        if not body:
            return {}
        response = await self._client.patch(
            f"/v0/inboxes/{inbox_id}/messages/{message_id}",
            json=body,
        )
        response.raise_for_status()
        return response.json()

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AgentMailClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
