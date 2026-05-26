"""WebSocket client connecting to the whatsappbridge Go companion service."""

from __future__ import annotations

import asyncio
import json
import logging
import os
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


# The Go bridge has a 1 MB WebSocket read limit.
# base64 adds ~33% overhead, so raw image bytes must stay well under 750 KB.
_BRIDGE_MAX_PAYLOAD_BYTES = 700_000
_WHATSAPP_MAX_DIMENSION = 1920


async def _prepare_media(path: str) -> str | None:
    """Resize/convert an image to fit WhatsApp and bridge WebSocket limits. Returns path to send."""
    import mimetypes

    mime = (mimetypes.guess_type(path)[0] or "").lower()
    if not mime.startswith("image/"):
        return path

    needs_resize = os.path.getsize(path) > _BRIDGE_MAX_PAYLOAD_BYTES
    if not needs_resize:
        try:
            from PIL import Image
            with Image.open(path) as img:
                w, h = img.size
            if max(w, h) <= _WHATSAPP_MAX_DIMENSION:
                return path
        except Exception:
            return path

    import functools
    import tempfile

    def _resize() -> str | None:
        from io import BytesIO
        from PIL import Image

        img = Image.open(path)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        # Scale down dimensions until file fits, starting from max allowed
        w, h = img.size
        dim = min(max(w, h), _WHATSAPP_MAX_DIMENSION)
        for quality in (85, 70, 55, 40):
            ratio = dim / max(w, h)
            scaled = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
            buf = BytesIO()
            scaled.save(buf, format="JPEG", quality=quality, optimize=True)
            if buf.tell() <= _BRIDGE_MAX_PAYLOAD_BYTES:
                tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                tmp.write(buf.getvalue())
                tmp.close()
                img.close()
                return tmp.name
            dim = int(dim * 0.75)

        img.close()
        return None

    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, functools.partial(_resize))
    except Exception:
        logger.exception("failed to resize image %s", path)
        return None


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

    async def send_media(self, chat_id: str, file_path: str, *, caption: str = "") -> str:
        """Send an image file to a WhatsApp chat."""
        import base64
        import mimetypes

        mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        with open(file_path, "rb") as f:
            data = base64.b64encode(f.read()).decode()

        request_id = str(uuid4())
        payload = {
            "type": "send_media",
            "id": request_id,
            "timestamp": utcnow().isoformat(),
            "payload": {
                "chat_id": chat_id,
                "mime_type": mime,
                "data": data,
                "caption": caption,
                "request_id": request_id,
            },
        }
        if self._ws is not None:
            await self._ws.send(json.dumps(payload))
        else:
            logger.warning("cannot send media, not connected to bridge")
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
            # Auto-seed an untrusted contact for unknown WhatsApp senders
            new_id = str(uuid4())
            now_iso = utcnow().isoformat()
            await self.db.execute(
                """INSERT INTO contacts (id, name, phone_number, is_trusted, created_at, updated_at)
                   VALUES (?, ?, ?, 0, ?, ?)""",
                (new_id, sender_name or phone_number, phone_number, now_iso, now_iso),
            )
            contact_id = new_id
            is_trusted = False
            logger.info("auto-seeded untrusted contact %s for phone %s", contact_id, phone_number)

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

        # Handle shared contacts — auto-seed into contacts table
        shared_contacts = payload.get("contacts", [])
        contacts_block = ""
        if shared_contacts:
            contacts_lines = ["## Shared Contacts"]
            for sc in shared_contacts:
                name = sc.get("display_name", "Unknown")
                phone = sc.get("phone", "")
                vcard = sc.get("vcard", "")
                # Auto-seed contact from shared vCard
                if phone:
                    normalized_phone = _jid_to_phone(phone)
                    existing = await self.db.fetch_one(
                        "SELECT id FROM contacts WHERE phone_number = ? AND deleted_at IS NULL LIMIT 1",
                        (normalized_phone,),
                    )
                    if not existing:
                        new_cid = str(uuid4())
                        await self.db.execute(
                            """INSERT INTO contacts (id, name, phone_number, is_trusted, created_at, updated_at)
                               VALUES (?, ?, ?, 0, ?, ?)""",
                            (new_cid, name, normalized_phone, now_iso, now_iso),
                        )
                        logger.info("auto-seeded shared contact %s (%s)", name, normalized_phone)
                    contacts_lines.append(f"- **{name}** — {normalized_phone}")
                else:
                    contacts_lines.append(f"- **{name}** (no phone)")
            contacts_block = "\n".join(contacts_lines)

        user_content = "\n".join([
            "## Incoming WhatsApp Message",
            f"From: {sender_name} ({sender_jid})" if sender_name else f"From: {sender_jid}",
            f"Chat: {chat_id} ({chat_kind})",
            f"Message ID: {wa_message_id}",
            "",
            text,
        ])
        if contacts_block:
            user_content += "\n\n" + contacts_block

        # Store user message immediately so queued messages are visible
        # to the next dispatch that acquires the session lock.
        from cyborg_server.services.session_service import SessionService
        await SessionService(self.ctx).add_message(
            session_key, "user", text,
            channel="whatsapp", sender_id=contact_id, dispatched=0,
        )

        # Check for active outreach request and inject into system prompt
        outreach_prompt = ""
        route_for_outreach = await self.db.fetch_one(
            "SELECT metadata FROM session_routes WHERE session_key = ?",
            (session_key,),
        )
        if route_for_outreach and route_for_outreach["metadata"]:
            try:
                route_meta = json.loads(route_for_outreach["metadata"])
            except (json.JSONDecodeError, TypeError):
                route_meta = {}
            if "outreach_initiated_from" in route_meta:
                outreach_prompt = (
                    "## Active Outreach Request\n"
                    "You proactively sent a message to this contact.\n"
                    f"- Requested by: {route_meta.get('outreach_requestor', 'unknown')}\n"
                    f"- Objective: {route_meta.get('outreach_objective', 'unknown')}\n"
                    f"- Your initial message: \"{route_meta.get('outreach_message', '')}\"\n\n"
                    "Your goal is to achieve the objective through this conversation. "
                    "When you have the information needed, call the finish_outreach tool to relay the result back."
                )

        # Load trusted-only memory index for trusted sessions
        memory_prompt = ""
        if is_trusted:
            from cyborg_server.services.memory_service import MemoryService
            mem_svc = MemoryService(self.ctx)
            trusted_wikis = await mem_svc.resolve_accessible_wikis(
                settings.harness.workspace_dir, session_key
            )
            # Filter to only trusted-access wikis (always-access ones are already in workspace_prompt)
            config = mem_svc.load_access_config(settings.harness.workspace_dir)
            trusted_only = [
                w for w in trusted_wikis
                if config.get("wikis", {}).get(w, {}).get("access") == "trusted"
            ]
            if trusted_only:
                memory_prompt = mem_svc.build_memory_index(
                    settings.harness.workspace_dir, trusted_only
                )

        system_content = "\n\n".join(
            p for p in (workspace_prompt, agenda, participants_prompt, outreach_prompt, memory_prompt) if p
        )

        logger.info("dispatching whatsapp message session=%s idempotency=%s", session_key, wa_message_id)

        from cyborg_server.services.llm_dispatch import LLMDispatchService
        from cyborg_server.services.tools import Tool
        from cyborg_server.services.tool_registry import build_common_tools

        wa_service = self

        # Core tools (workspace, memory, docs, changelog, email_send, contact, phone, reflection, delegation)
        tools = build_common_tools(self.ctx, session_key=session_key, is_trusted=is_trusted)

        # WhatsApp-specific: outreach tools for trusted contacts
        if contact_id and is_trusted:
            from cyborg_server.services.whatsapp_outreach_tools import make_whatsapp_outreach_tools
            tools.extend(make_whatsapp_outreach_tools(self.ctx, self, session_key))

        # Outreach reply tool for active outreach targets
        route = await self.db.fetch_one(
            "SELECT metadata FROM session_routes WHERE session_key = ?",
            (session_key,),
        )
        if route and route["metadata"]:
            try:
                meta = json.loads(route["metadata"])
            except (json.JSONDecodeError, TypeError):
                meta = {}
            if "outreach_initiated_from" in meta:
                from cyborg_server.services.whatsapp_outreach_tools import make_outreach_reply_tools
                tools.extend(make_outreach_reply_tools(self.ctx, self, session_key))

        message_was_sent = [False]
        sent_texts: list[str] = []

        async def _send_whatsapp_message(text: str) -> str:
            """Send a reply message to the WhatsApp chat."""
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

        async def _send_whatsapp_media(workspace_path: str, caption: str = "") -> str:
            """Send an image or media file to the current WhatsApp chat."""
            workspace = settings.harness.workspace_dir.expanduser().resolve()
            resolved = (workspace / workspace_path).resolve()
            if not str(resolved).startswith(str(workspace)):
                return f"Error: path escapes workspace"
            if not resolved.is_file():
                return f"Error: file not found: {workspace_path}"
            prepared = await _prepare_media(str(resolved))
            if prepared is None:
                return "Error: failed to prepare media for sending"
            message_was_sent[0] = True
            if caption:
                sent_texts.append(f"[Image: {caption}]")
            else:
                sent_texts.append(f"[Image: {resolved.name}]")
            request_id = await wa_service.send_media(chat_id, prepared, caption=caption)
            return f"Media sent (request_id={request_id})"

        tools.append(Tool(
            name="send_whatsapp_media",
            description=(
                "Send an image or media file to the current WhatsApp chat. "
                "Provide a path relative to the workspace directory."
            ),
            parameters={
                "workspace_path": {"type": "string", "description": "Path to the image file, relative to the workspace directory."},
                "caption": {"type": "string", "description": "Optional caption for the image."},
            },
            required=["workspace_path"],
            handler=_send_whatsapp_media,
        ))

        dispatch_id = str(uuid4())

        async def _run_dispatch() -> str:
            from cyborg_server.services.session_service import SessionService
            from cyborg_server.services.session_dispatch_gate import SessionDispatchGate

            session_svc = SessionService(self.ctx)
            async with SessionDispatchGate.get_lock(session_key):
                claimed = await session_svc.mark_dispatched(session_key)
                if claimed == 0:
                    return ""

                messages = await build_chat_messages(
                    None, session_key,
                    db=self.db,
                    system_content=system_content,
                    max_history=20,
                )

                result = await LLMDispatchService(self.ctx).chat_with_tools(
                    messages, tools,
                    call_category="whatsapp_incoming",
                    session_key=session_key,
                    dispatch_id=dispatch_id,
                    contact_id=contact_id,
                )
                # Tap: if LLM produced text but didn't use send_whatsapp_message,
                # give it a second chance with a reminder.
                if not message_was_sent[0] and result.strip():
                    from cyborg_server.services.tap import tap_dispatch
                    result = await tap_dispatch(
                        self.ctx, messages=messages, tools=tools,
                        session_key=session_key,
                        send_tool_name="send_whatsapp_message",
                        first_result=result,
                        call_category="whatsapp_incoming",
                        dispatch_id=dispatch_id,
                        contact_id=contact_id,
                    )
                # Record to unified session history — combine LLM text output + all sent messages
                # If nothing was sent and the result is just a NO_REPLY variant, skip recording
                # to avoid poisoning future decisions with a pattern of non-responses.
                parts = [p for p in ([result] if result.strip() else []) + sent_texts if p.strip()]
                assistant_text = "\n\n".join(parts) if parts else result
                if not message_was_sent[0] and assistant_text.strip().upper().rstrip(".") in (
                    "NO_REPLY", "NO_REPLY", "NO REPLY", "NOTHING TO SAY",
                ):
                    pass  # Don't record NO_REPLY to session history
                else:
                    await session_svc.add_message(session_key, "assistant", assistant_text, channel="whatsapp")
                if self.ctx.event_bus:
                    await self.ctx.event_bus.publish("whatsapp.message.received", {
                        "session_key": session_key,
                        "sender_name": sender_name,
                        "chat_kind": chat_kind,
                        "text_preview": text[:100],
                    })
                return result

        asyncio.create_task(_run_dispatch())
