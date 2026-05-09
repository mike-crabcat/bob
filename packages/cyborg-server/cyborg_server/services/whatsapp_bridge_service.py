"""WebSocket client connecting to the whatsappbridge Go companion service."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import uuid4

import websockets

from fastapi import HTTPException

from cyborg_server.config import Settings
from cyborg_server.context import AppContext
from cyborg_server.models import SessionRouteCreate, SessionRouteKind
from cyborg_server.services.base import BaseService, utcnow
from cyborg_server.services.session_route_service import SessionRouteService

logger = logging.getLogger(__name__)

WHATSAPP_INCOMING_AGENDA = """\
You are managing a WhatsApp conversation. An incoming message has been received.

Your role: read the message and respond appropriately.
Use `cyborg whatsapp send --chat-id {chat_id} --text "<your reply>"` to respond.
Keep your response concise and natural for a messaging context.
"""


class WhatsAppBridgeService(BaseService):
    """WebSocket client connecting to the whatsappbridge Go companion service."""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._task: asyncio.Task | None = None
        self._connected = False
        self._last_bridge_status: dict[str, Any] = {}
        self._last_qr_code: str | None = None
        self._last_pairing_code: str | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        settings = self._get_settings()
        if not settings.whatsapp_bridge.enabled:
            return
        self._task = asyncio.create_task(self._run_loop(), name="whatsapp_bridge")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        self._connected = False

    async def send_message(self, chat_id: str, text: str, *, reply_to: str | None = None) -> str:
        request_id = str(uuid4())
        payload = {
            "type": "send_message",
            "id": request_id,
            "timestamp": utcnow().isoformat(),
            "payload": {
                "chat_id": chat_id,
                "text": text,
                "reply_to_message_id": reply_to,
                "request_id": request_id,
            },
        }
        if self._ws is not None:
            await self._ws.send(json.dumps(payload))
        else:
            logger.warning("cannot send message, not connected to bridge")
        return request_id

    async def request_pairing(self, *, method: str = "qr", phone_number: str | None = None) -> dict[str, Any]:
        msg_id = str(uuid4())
        payload = {
            "type": "request_pairing",
            "id": msg_id,
            "timestamp": utcnow().isoformat(),
            "payload": {
                "method": method,
                "phone_number": phone_number or "",
            },
        }
        if self._ws is not None:
            await self._ws.send(json.dumps(payload))
            return {"status": "requested", "method": method}
        raise HTTPException(status_code=503, detail="Not connected to bridge")

    async def get_bridge_status(self) -> dict[str, Any]:
        result = {
            "bridge_connected": self._connected,
            **self._last_bridge_status,
            "last_qr_code": self._last_qr_code,
            "last_pairing_code": self._last_pairing_code,
        }
        # Also fetch live pairing info from bridge's HTTP endpoint
        try:
            settings = self._get_settings()
            from urllib.request import urlopen, Request
            bridge_url = settings.whatsapp_bridge.url.replace("ws://", "http://").replace("/ws", "/pairing")
            req = Request(bridge_url)
            with urlopen(req, timeout=5) as resp:
                pairing = json.loads(resp.read())
                if pairing.get("qr_code"):
                    result["last_qr_code"] = pairing["qr_code"]
                if pairing.get("pairing_code"):
                    result["last_pairing_code"] = pairing["pairing_code"]
        except Exception:
            pass
        return result

    async def _run_loop(self) -> None:
        settings = self._get_settings()
        while True:
            try:
                url = settings.whatsapp_bridge.url
                token = settings.whatsapp_bridge.token
                connect_url = f"{url}?token={token}" if token else url

                async with websockets.connect(connect_url) as ws:
                    self._ws = ws
                    self._connected = True
                    logger.info("connected to whatsapp bridge at %s", url)

                    async for raw in ws:
                        try:
                            await self._on_message(json.loads(raw))
                        except Exception:
                            logger.exception("error handling bridge message")

            except asyncio.CancelledError:
                raise
            except Exception:
                self._connected = False
                self._ws = None
                logger.warning(
                    "whatsapp bridge connection lost, reconnecting in %ss",
                    settings.whatsapp_bridge.reconnect_interval_seconds,
                    exc_info=True,
                )
                await asyncio.sleep(settings.whatsapp_bridge.reconnect_interval_seconds)

    async def _on_message(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type", "")
        payload = msg.get("payload", {})

        if msg_type == "whatsapp.connected":
            logger.info("whatsapp connected via bridge")
        elif msg_type == "whatsapp.disconnected":
            logger.warning("whatsapp disconnected: %s", payload.get("reason", "unknown"))
        elif msg_type == "whatsapp.qr_code":
            self._last_qr_code = payload.get("qr_string", "")
            logger.info("whatsapp QR code available (expires %s)", payload.get("expires_at", ""))
        elif msg_type == "whatsapp.pairing_code":
            self._last_pairing_code = payload.get("code", "")
            logger.info("whatsapp pairing code: %s", payload.get("code", ""))
        elif msg_type == "whatsapp.incoming_message":
            await self._handle_incoming_message(payload)
        elif msg_type == "whatsapp.message_acked":
            pass
        elif msg_type == "send_message_result":
            if not payload.get("success"):
                logger.warning("send message failed: %s (request %s)", payload.get("error"), payload.get("request_id"))
        elif msg_type == "bridge.status":
            self._last_bridge_status = payload
        else:
            logger.debug("unknown bridge message type: %s", msg_type)

    async def _handle_incoming_message(self, payload: dict[str, Any]) -> None:
        settings = self._get_settings()
        if not settings.openclaw.enabled:
            logger.info("openclaw not configured, skipping dispatch for whatsapp message")
            return

        chat_id = payload.get("chat_id", "")
        chat_kind = payload.get("chat_kind", "dm")
        sender_jid = payload.get("sender_jid", "")
        sender_name = payload.get("sender_name", "")
        text = payload.get("text", "")
        wa_message_id = payload.get("whatsapp_message_id", "")

        if not text:
            return

        # Derive session key
        agent_id = settings.openclaw.agent_id or "main"
        # Extract phone number from JID (e.g., "1234567890@s.whatsapp.net" -> "+1234567890")
        phone_part = sender_jid.split("@")[0] if "@" in sender_jid else sender_jid
        session_key = f"agent:{agent_id}:whatsapp:{chat_kind}:{phone_part}"

        # Resolve or create session route
        route_service = SessionRouteService(self.ctx)
        await route_service.create_route(SessionRouteCreate(
            channel="whatsapp",
            session_key=session_key,
            kind=SessionRouteKind.THREAD if chat_kind == "group" else SessionRouteKind.DM,
            chat_id=chat_id,
            metadata={
                "sender_jid": sender_jid,
                "sender_name": sender_name,
            },
        ))

        # Build prompt following email pattern
        agenda = WHATSAPP_INCOMING_AGENDA.format(chat_id=chat_id)
        prompt_parts = [
            agenda,
            "",
            "## Incoming WhatsApp Message",
            f"From: {sender_name} ({sender_jid})" if sender_name else f"From: {sender_jid}",
            f"Chat: {chat_id} ({chat_kind})",
            f"Message ID: {wa_message_id}",
            "",
            text,
            "",
            "## Instructions",
            "Review this message and respond if appropriate.",
            f"Use `cyborg whatsapp send --chat-id {chat_id} --text \"<your reply>\"` to respond.",
        ]
        prompt = "\n".join(prompt_parts)

        logger.info(
            "dispatching whatsapp message to openclaw session=%s idempotency=%s",
            session_key, wa_message_id,
        )

        # Dispatch to OpenClaw following email pattern
        from cyborg_server.services.openclaw_hook_service import OpenClawHookService
        from cyborg_server.services.prompt_history import log_prompt
        from cyborg_server.services.dispatch_service import DispatchService

        hook_service = OpenClawHookService(
            self.ctx,
            cyborg_service_url=settings.resolved_public_url,
        )

        await log_prompt(
            self.db,
            category="whatsapp_incoming",
            prompt_text=prompt,
            session_key=session_key,
        )
        dispatch_id = await DispatchService(self.ctx).record_dispatch(
            notification_type="whatsapp_incoming",
            session_key=session_key,
        )

        DispatchService(self.ctx).track(
            dispatch_id,
            hook_service._send_gateway_request(
                "agent",
                {
                    "message": prompt,
                    "deliver": False,
                    "sessionKey": session_key,
                    "thinking": "on",
                    "timeout": 3600,
                    "idempotencyKey": wa_message_id,
                },
                expect_final=True,
                timeout_seconds=300,
            ),
        )
