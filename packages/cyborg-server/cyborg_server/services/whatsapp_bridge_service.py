"""WebSocket client connecting to the whatsappbridge Go companion service."""

from __future__ import annotations

import asyncio
import json
import logging
import re
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


def _jid_to_phone(jid: str) -> str:
    """Extract phone number from WhatsApp JID and normalize to +CC format."""
    phone_part = jid.split("@")[0] if "@" in jid else jid
    digits = re.sub(r"\D", "", phone_part)
    if phone_part.startswith("+"):
        return "+" + digits
    # Assume Australian number if no country code
    if digits.startswith("0"):
        return "+61" + digits[1:]
    if digits.startswith("61"):
        return "+" + digits
    if len(digits) > 8:
        return "+" + digits
    return "+" + digits


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

    async def _send_ack(self, message_id: str) -> None:
        if self._ws is None:
            return
        payload = {
            "type": "ack",
            "id": str(uuid4()),
            "timestamp": utcnow().isoformat(),
            "payload": {"message_id": message_id},
        }
        try:
            await self._ws.send(json.dumps(payload))
        except Exception:
            logger.warning("failed to send ack for %s", message_id, exc_info=True)

    async def _build_participants_prompt(self, session_key: str) -> str:
        rows = await self.db.fetch_all(
            "SELECT display_name, identifier, contact_id, is_trusted, last_active_at "
            "FROM session_participants WHERE session_key = ? ORDER BY last_active_at DESC",
            (session_key,),
        )
        if not rows:
            return ""
        lines = ["## Participants"]
        for r in rows:
            name = r["display_name"] or r["identifier"]
            if r["contact_id"]:
                trust = "trusted" if r["is_trusted"] else "untrusted"
                lines.append(f"- {name} (contact, {trust})")
            else:
                lines.append(f"- {name} ({r['identifier']}, not in contacts)")
        return "\n".join(lines)

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
        if not settings.openai.enabled:
            logger.info("No LLM provider configured, skipping dispatch for whatsapp message")
            return

        chat_id = payload.get("chat_id", "")
        chat_kind = payload.get("chat_kind", "dm")
        sender_jid = payload.get("sender_jid", "")
        sender_name = payload.get("sender_name", "")
        text = payload.get("text", "")
        wa_message_id = payload.get("whatsapp_message_id", "")

        # Ack receipt so the bridge clears it from the incoming queue
        await self._send_ack(wa_message_id)

        if not text:
            return

        logger.info(
            "incoming whatsapp message: chat_id=%s chat_kind=%s sender_jid=%s sender_name=%s",
            chat_id, chat_kind, sender_jid, sender_name,
        )

        # Resolve contact — use chat_id for DMs (sender_jid may be device JID for own messages)
        phone_jid = chat_id if chat_kind == "dm" else sender_jid
        phone_number = _jid_to_phone(phone_jid)
        contact_id = None
        is_trusted = False
        contact = await self.db.fetch_one(
            "SELECT id, is_trusted FROM contacts WHERE phone_number = ? AND deleted_at IS NULL LIMIT 1",
            (phone_number,),
        )
        if contact:
            contact_id = contact["id"]
            is_trusted = bool(contact.get("is_trusted", 0))
            logger.info("resolved contact %s (trusted=%s) for phone %s", contact_id, is_trusted, phone_number)
        else:
            logger.info("no contact found for phone %s", phone_number)

        # Derive session key
        agent_id = "main"
        if chat_kind == "group":
            key_part = chat_id.split("@")[0] if "@" in chat_id else chat_id
        else:
            key_part = sender_jid.split("@")[0] if "@" in sender_jid else sender_jid
        session_key = f"agent:{agent_id}:whatsapp:{chat_kind}:{key_part}"

        # Upsert sender as session participant
        now_iso = utcnow().isoformat()
        await self.db.execute(
            """INSERT INTO session_participants (session_key, identifier, display_name, contact_id, is_trusted, last_active_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_key, identifier) DO UPDATE SET
                   display_name = excluded.display_name,
                   contact_id = COALESCE(excluded.contact_id, session_participants.contact_id),
                   is_trusted = CASE WHEN excluded.contact_id IS NOT NULL THEN excluded.is_trusted ELSE session_participants.is_trusted END,
                   last_active_at = excluded.last_active_at""",
            (session_key, phone_number, sender_name or phone_number,
             contact_id, 1 if is_trusted else 0, now_iso),
        )

        # Create session route — DM needs contact_id, group needs chat_id
        route_service = SessionRouteService(self.ctx)
        from cyborg_server.exceptions import ConflictError
        try:
            if chat_kind == "group":
                await route_service.create_route(SessionRouteCreate(
                    channel="whatsapp",
                    session_key=session_key,
                    kind=SessionRouteKind.GROUP,
                    chat_id=chat_id,
                    metadata={
                        "sender_jid": sender_jid,
                        "sender_name": sender_name,
                    },
                ))
            else:
                if contact_id is None:
                    logger.warning("dropping WhatsApp DM from unknown contact %s (no contact_id for session route)", phone_number)
                    return
                await route_service.create_route(SessionRouteCreate(
                    channel="whatsapp",
                    session_key=session_key,
                    kind=SessionRouteKind.DM,
                    contact_id=contact_id,
                    metadata={
                        "sender_jid": sender_jid,
                        "sender_name": sender_name,
                    },
                ))
        except ConflictError:
            pass  # Route already exists, proceed with dispatch

        # Resolve agenda
        from cyborg_server.services.session_agenda_service import SessionAgendaService
        agenda_svc = SessionAgendaService(self.ctx)
        agenda = await agenda_svc.get_effective_agenda(
            session_key, "whatsapp",
            contact_id=contact_id, is_trusted=is_trusted,
        )

        # Build system prompt: workspace context + agenda + participants
        from cyborg_server.services.prompt_assembler import load_workspace_prompt, build_chat_messages
        workspace_prompt = load_workspace_prompt(settings.harness.workspace_dir)

        participants_prompt = await self._build_participants_prompt(session_key)

        user_content = "\n".join([
            "## Incoming WhatsApp Message",
            f"From: {sender_name} ({sender_jid})" if sender_name else f"From: {sender_jid}",
            f"Chat: {chat_id} ({chat_kind})",
            f"Message ID: {wa_message_id}",
            "",
            text,
        ])
        messages = await build_chat_messages(
            user_content,
            session_key,
            db=self.db,
            system_content="\n\n".join(p for p in (workspace_prompt, agenda, participants_prompt) if p),
            max_history=20,
        )

        logger.info("dispatching whatsapp message session=%s idempotency=%s", session_key, wa_message_id)

        from cyborg_server.services.dispatch_service import DispatchService
        from cyborg_server.services.llm_dispatch import LLMDispatchService
        from cyborg_server.services.tools import Tool
        from cyborg_server.services.workspace_tools import make_workspace_tools

        # Build tools: workspace file access + whatsapp reply
        tools = make_workspace_tools(self.ctx, session_key=session_key)
        wa_service = self

        # Add outreach tools for trusted DM contacts
        if contact_id and is_trusted and chat_kind == "dm":
            from cyborg_server.services.whatsapp_outreach_tools import make_whatsapp_outreach_tools
            tools.extend(make_whatsapp_outreach_tools(self.ctx, self, session_key))
            if settings.harness.skill_dev_enabled:
                from cyborg_server.services.delegation_tools import make_delegation_tools
                tools.extend(make_delegation_tools(self.ctx, session_key))

        message_was_sent = [False]

        async def _send_whatsapp_message(text: str) -> str:
            """Send a reply message to the WhatsApp chat.
            If you do not want to reply, send "NO_REPLY" as the text."""
            message_was_sent[0] = True
            if text.strip().upper() == "NO_REPLY":
                return "No reply sent."
            request_id = await wa_service.send_message(chat_id, text)
            return f"Message sent (request_id={request_id})"

        tools.append(Tool(
            name="send_whatsapp_message",
            description="Send a reply message to the current WhatsApp conversation.",
            parameters={"text": {"type": "string", "description": "The message text to send."}},
            required=["text"],
            handler=_send_whatsapp_message,
        ))

        dispatch_id = await DispatchService(self.ctx).record_dispatch(
            notification_type="whatsapp_incoming",
            session_key=session_key,
        )

        async def _run_dispatch() -> str:
            from cyborg_server.services.session_service import SessionService
            result = await LLMDispatchService(self.ctx).chat_with_tools(
                messages, tools,
                call_category="whatsapp_incoming",
                session_key=session_key,
                dispatch_id=dispatch_id,
            )
            # Auto-send if the LLM generated text but didn't use the tool
            if not message_was_sent[0] and result:
                logger.info("LLM did not use send_whatsapp_message, auto-sending response")
                await wa_service.send_message(chat_id, result)
            # Record to unified session history
            session_svc = SessionService(self.ctx)
            await session_svc.add_message(session_key, "user", text, channel="whatsapp", sender_id=contact_id)
            await session_svc.add_message(session_key, "assistant", result, channel="whatsapp")
            return result

        DispatchService(self.ctx).track(dispatch_id, _run_dispatch())
