"""WebSocket client connecting to the whatsappbridge Go companion service."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import websockets

from fastapi import HTTPException

from server.config import Settings
from server.context import AppContext
from server.services.base import BaseService, utcnow
from server.services.openai_service import strip_citation_markers
from server.services.whatsapp_bridge_service._media import (
    _jid_to_phone,
    _prepare_media,
    resolve_sendable_media,
)

logger = logging.getLogger(__name__)

# Rate limiting for quota-exhaustion notifications: session_key -> monotonic
# timestamp of the last notification sent. Resets on process restart, which is
# fine — if Bob restarts, credit may have been topped up in the meantime.
_quota_notify_last: dict[str, float] = {}
_QUOTA_NOTIFY_MIN_INTERVAL = 3600.0  # 1 hour


from server.services.dispatch_runner import _is_quota_error, is_no_reply


async def _notify_quota_exhausted(wa_service: Any, chat_id: str, session_key: str) -> None:
    """Send a one-line 'out of credit' notice to the chat, at most once per hour."""
    now = time.monotonic()
    last = _quota_notify_last.get(session_key, 0.0)
    if now - last < _QUOTA_NOTIFY_MIN_INTERVAL:
        return
    _quota_notify_last[session_key] = now
    try:
        await wa_service.send_message(
            chat_id,
            "I'm out of OpenAI credit — I'll reply again as soon as it's topped up.",
        )
        logger.warning("quota notify: sent to %s", session_key)
    except Exception:
        # Don't let a notification failure mask the original quota error.
        logger.warning("quota notify: failed to send to %s", session_key, exc_info=True)


def _copy_document_to_workspace(settings: Settings, src: Path, msg_id: str) -> str | None:
    """Copy a received document into workspace/whatsapp_media/ and return the
    workspace-relative path. Prefixes with a short slice of the WhatsApp message
    ID so repeat filenames from different senders don't overwrite each other."""
    workspace = settings.harness.workspace_dir.expanduser().resolve()
    dest_dir = workspace / "whatsapp_media"
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w.\-]", "_", src.name) or "document"
    short_id = (msg_id or "").split("_")[-1][:8] or uuid4().hex[:8]
    dest = dest_dir / f"{short_id}_{safe_name}"
    try:
        shutil.copy2(src, dest)
    except Exception:
        logger.exception("failed to copy document %s into workspace", src)
        return None
    return str(dest.relative_to(workspace))




from server.services.whatsapp_bridge_service._group_events import GroupEventsMixin
from server.services.whatsapp_bridge_service._slash_commands import SlashCommandsMixin


class WhatsAppBridgeService(BaseService, GroupEventsMixin, SlashCommandsMixin):
    """WebSocket client connecting to the whatsappbridge Go companion service."""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._task: asyncio.Task | None = None
        self._connected = False
        self._last_bridge_status: dict[str, Any] = {}
        self._last_qr_code: str | None = None
        self._last_pairing_code: str | None = None
        self._verbose_queue: asyncio.Queue[dict[str, Any]] | None = None
        self._verbose_listener_task: asyncio.Task | None = None
        self._presence_subscribed: set[str] = set()
        # Live typing-indicator keepalives: chat_id -> asyncio.Task. Strong
        # refs on purpose — asyncio holds only weak task refs, so the dict is
        # what keeps an in-flight keepalive alive (same lesson as backburner's
        # _supervisors). Empty = no chat shows Bob typing.
        self._typing_tasks: dict[str, asyncio.Task] = {}
        self._register_send_executors()

    def _register_send_executors(self) -> None:
        """Bind this instance as the executor for WhatsApp send effects
        (Bob3 Phase IV outbox). Last-constructed instance wins — there is
        one live bridge per process."""
        from server.services import effects as effects_svc

        async def _exec_send(ctx: Any, payload: dict[str, Any]) -> str:
            return await self.send_message(
                payload["chat_id"], payload["text"],
                reply_to=payload.get("reply_to"))

        async def _exec_media(ctx: Any, payload: dict[str, Any]) -> str:
            return await self.send_media(
                payload["chat_id"], payload["file_path"],
                caption=payload.get("caption", ""))

        effects_svc.register_executor("whatsapp_send", _exec_send)
        effects_svc.register_executor("whatsapp_send_media", _exec_media)

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        settings = self._get_settings()
        if not settings.whatsapp_bridge.enabled:
            return
        self._task = asyncio.create_task(self._run_loop(), name="whatsapp_bridge")

        # Bob3 Phase III recovery: re-arm dispatch for sessions whose stored
        # messages never dispatched (crash during an armed attention window).
        async def _recovery_sweep() -> None:
            try:
                await asyncio.sleep(10)  # let the bridge connect first
                resumed = await self.resume_pending_sessions()
                if resumed:
                    logger.info("recovery sweep re-armed %d session(s)", resumed)
            except Exception:
                logger.warning("recovery sweep failed", exc_info=True)

        self._recovery_task = asyncio.create_task(_recovery_sweep(), name="wa_recovery_sweep")

        # Post-call drain (Bob3 Phase VI item 6): when a live call ends,
        # occupancy wakes the conversation so queued messages run as one turn.
        from server.services import occupancy
        occupancy.set_drain(self.wake_session)

        # Subscribe to memory verbose notices and forward to WhatsApp.
        if self.ctx.event_bus:
            self._verbose_queue = self.ctx.event_bus.subscribe()
            self._verbose_listener_task = asyncio.create_task(
                self._verbose_event_loop(), name="verbose_listener"
            )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._verbose_listener_task is not None:
            self._verbose_listener_task.cancel()
            try:
                await self._verbose_listener_task
            except asyncio.CancelledError:
                pass
            self._verbose_listener_task = None
        if self._verbose_queue is not None and self.ctx.event_bus:
            self.ctx.event_bus.unsubscribe(self._verbose_queue)
            self._verbose_queue = None
        # Reap any live typing keepalives before the socket goes away.
        for task in self.__dict__.get("_typing_tasks", {}).values():
            task.cancel()
        self.__dict__.get("_typing_tasks", {}).clear()
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
        """Send a media file to a WhatsApp chat.

        Always uploads via HTTP /upload, then sends a small WS message referencing
        the upload_id. Avoids the Go bridge's ~1MiB WebSocket frame cap, which
        capped inline base64 sends at ~770KB and killed the WS session on overflow.
        """
        import mimetypes

        mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        request_id = str(uuid4())
        upload_id = await self._upload_media(file_path, mime, caption)

        payload = {
            "type": "send_media",
            "id": request_id,
            "timestamp": utcnow().isoformat(),
            "payload": {
                "chat_id": chat_id,
                "mime_type": mime,
                "upload_id": upload_id,
                "caption": caption,
                "request_id": request_id,
            },
        }
        if self._ws is not None:
            await self._ws.send(json.dumps(payload))
        else:
            logger.warning("cannot send media, not connected to bridge")
        return request_id

    async def _upload_media(self, file_path: str, mime: str, caption: str) -> str:
        """POST a file to the bridge's /upload endpoint and return its upload_id."""
        import os

        import httpx

        settings = self._get_settings()
        ws_url = settings.whatsapp_bridge.url  # e.g. ws://127.0.0.1:8430/ws
        http_url = (
            ws_url.replace("ws://", "http://")
            .replace("wss://", "https://")
            .replace("/ws", "/upload")
        )
        token = settings.whatsapp_bridge.token
        with open(file_path, "rb") as f:
            resp = await httpx.AsyncClient().post(
                http_url,
                params={"token": token} if token else {},
                files={"file": (os.path.basename(file_path), f, mime)},
                data={"mime_type": mime, "caption": caption},
                timeout=120.0,
            )
        resp.raise_for_status()
        return resp.json()["upload_id"]

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

    async def _verbose_event_loop(self) -> None:
        """Listen for memory.verbose_notice events and forward to WhatsApp.

        Filters to WhatsApp-routed sessions; resolves the chat_id via
        bindings and calls send_message. Silently drops events for
        other transports.
        """
        assert self._verbose_queue is not None
        try:
            while True:
                event = await self._verbose_queue.get()
                if event.get("type") != "memory.verbose_notice":
                    continue
                payload = event.get("payload", {})
                session_key = payload.get("session_key", "")
                if ":whatsapp:" not in session_key:
                    continue
                text = payload.get("text", "")
                if not text:
                    continue
                try:
                    from server.repositories.conversations import (
                        ConversationRepository, wa_send_jid)
                    route = await ConversationRepository(self.db).route_for(session_key)
                    jid = wa_send_jid(route["address"]) if route and route["is_active"] else None
                    if not jid:
                        continue
                    await self.send_message(jid, text)
                except Exception:
                    logger.exception("failed to forward verbose notice for %s", session_key)
        except asyncio.CancelledError:
            pass

    async def _lookup_contact(self, phone_number: str) -> tuple[str, bool] | None:
        """Return (contact_id, is_trusted) for an existing contact, or None.

        Fuzzy: exact match first, then prefix-match fallback for WhatsApp JIDs
        with extra trailing digits (see ContactRepository.get_by_phone_fuzzy).
        """
        from server.repositories.contacts import ContactRepository
        contact = await ContactRepository(self.db).get_by_phone_fuzzy(phone_number)
        if contact:
            return contact["id"], bool(contact.get("is_trusted", 0))
        return None

    async def _resolve_or_seed_contact(self, phone_number: str, display_name: str = "") -> tuple[str, bool]:
        """Find an existing contact by phone or auto-seed an untrusted one. Returns (contact_id, is_trusted)."""
        existing = await self._lookup_contact(phone_number)
        if existing:
            return existing
        from server.services.channel_policies import ContactResolver
        new_id = await ContactResolver(self.ctx).seed_untrusted_by_phone(
            phone_number, display_name)
        return new_id, False


    async def subscribe_presence(self, chat_id: str) -> None:
        """Request the bridge to subscribe to presence for a chat."""
        payload = {
            "type": "subscribe_presence",
            "id": str(uuid4()),
            "timestamp": utcnow().isoformat(),
            "payload": {"chat_id": chat_id},
        }
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps(payload))
            except Exception:
                logger.debug("failed to send presence subscription for %s", chat_id)

    async def _send_chat_presence(self, chat_id: str, state: str, media: str = "") -> None:
        """Fire-and-forget outbound chat-presence frame. Never raises — the
        typing indicator is cosmetic and must not break a turn. Self-heals
        across bridge reconnects: every call re-reads the current socket, so
        the keepalive resumes on its next tick after a reconnect."""
        if self._ws is None:
            return
        payload = {
            "type": "send_chat_presence",
            "id": str(uuid4()),
            "timestamp": utcnow().isoformat(),
            "payload": {"chat_id": chat_id, "state": state, "media": media},
        }
        try:
            await self._ws.send(json.dumps(payload))
        except Exception:
            logger.debug("failed to send chat presence %s for %s", state, chat_id)

    async def start_typing(self, chat_id: str) -> None:
        """Show the WhatsApp typing indicator for a chat and keep it alive
        until stop_typing. No-raise: cosmetic, must never break a turn.

        Chat presence is ephemeral signal, deliberately OUTSIDE the effects
        outbox and the bridge's outgoing queue — nothing durable, nothing
        retried; a process crash just lets the indicator fade client-side.
        """
        try:
            wa = self._get_settings().whatsapp_bridge
            if not getattr(wa, "typing_indicator_enabled", False):
                return
            # __dict__.setdefault: tests and BaseService.from_db construct
            # instances without __init__.
            tasks = self.__dict__.setdefault("_typing_tasks", {})
            old = tasks.get(chat_id)
            if old is not None and not old.done():
                old.cancel()  # back-to-back turns: restart the cap clock
            await self._send_chat_presence(chat_id, "composing")

            async def _keepalive(
                interval: float = wa.typing_keepalive_seconds,
                cap: float = wa.typing_max_seconds,
            ) -> None:
                # Clients time the composing indicator out, so re-assert it
                # until the turn settles. The cap is the leak backstop for a
                # lost settle hook; task.cancel() from stop_typing is the
                # normal exit.
                loop = asyncio.get_running_loop()
                deadline = loop.time() + cap
                try:
                    while loop.time() < deadline:
                        await asyncio.sleep(interval)
                        await self._send_chat_presence(chat_id, "composing")
                except asyncio.CancelledError:
                    pass

            tasks[chat_id] = asyncio.create_task(
                _keepalive(), name=f"wa-typing:{chat_id}")
        except Exception:
            logger.debug("start_typing failed for %s", chat_id, exc_info=True)

    async def stop_typing(self, chat_id: str) -> None:
        """Clear the typing indicator: cancel the keepalive and send 'paused'
        once iff one was active. No start → clean no-op. No-raise."""
        try:
            task = self.__dict__.setdefault("_typing_tasks", {}).pop(chat_id, None)
            if task is not None and not task.done():
                # Deliberately NOT awaited: this runs inside the dispatch
                # runner's finally, where awaiting a cancelled task can
                # re-raise CancelledError into a normal exit path.
                task.cancel()
                await self._send_chat_presence(chat_id, "paused")
        except Exception:
            logger.debug("stop_typing failed for %s", chat_id, exc_info=True)

    async def _handle_chat_presence(self, payload: dict[str, Any]) -> None:
        """Handle typing/presence events from the bridge."""
        chat_id = payload.get("chat_id", "")
        sender_jid = payload.get("sender_jid", "")
        sender_name = payload.get("sender_name", "")
        if not chat_id or not sender_jid:
            return

        chat_kind = "group" if "@g.us" in chat_id else "dm"
        agent_id = "main"
        if chat_kind == "group":
            key_part = chat_id.split("@")[0]
        else:
            key_part = sender_jid.split("@")[0].split(":")[0]
        session_key = f"agent:{agent_id}:whatsapp:{chat_kind}:{key_part}"

        # Typing extends an armed attention window (Tier 1 presence awareness).
        from server.services.attention import AttentionCoordinator
        AttentionCoordinator(self.ctx).notify_typing(session_key, sender_name or "")


    async def _on_message(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type", "")
        payload = msg.get("payload", {})
        if msg_type not in ("whatsapp.incoming_message", "whatsapp.message_acked", "bridge.status"):
            logger.info("bridge message: type=%s", msg_type)

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
        elif msg_type == "whatsapp.group_member_change":
            await self._handle_group_member_change(payload)
        elif msg_type == "whatsapp.group_sync":
            await self._handle_group_sync(payload)
        elif msg_type == "whatsapp.chat_presence":
            await self._handle_chat_presence(payload)
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
        mentioned_jids = payload.get("mentioned_jids", [])
        media = payload.get("media")

        # Resolve media path from media metadata. Image, video, GIF, and
        # documents all land in the same bridge media dir.
        image_path: str | None = None
        image_mime_type: str = "image/jpeg"
        video_path: str | None = None
        video_mime_type: str = "video/mp4"
        is_gif = False
        document_path: str | None = None
        document_workspace_path: str | None = None
        document_filename: str = ""
        if media:
            media_type = media.get("media_type")
            media_dir = settings.whatsapp_bridge.media_dir.expanduser().resolve()
            filename = media.get("filename", "")
            if filename and media_type in ("image", "video", "gif", "document"):
                resolved = (media_dir / filename).resolve()
                if str(resolved).startswith(str(media_dir)) and resolved.is_file():
                    if media_type == "image":
                        image_path = str(resolved)
                        image_mime_type = media.get("mime_type", "image/jpeg")
                    elif media_type == "document":
                        document_path = str(resolved)
                        document_filename = filename
                        document_workspace_path = _copy_document_to_workspace(
                            settings, resolved, wa_message_id,
                        )
                    else:
                        video_path = str(resolved)
                        video_mime_type = media.get("mime_type", "video/mp4")
                        is_gif = media_type == "gif"

        # Ack receipt so the bridge clears it from the incoming queue
        await self._send_ack(wa_message_id)

        if not text and not image_path and not video_path and not document_path:
            return

        logger.info(
            "incoming whatsapp message: chat_id=%s chat_kind=%s sender_jid=%s sender_name=%s",
            chat_id, chat_kind, sender_jid, sender_name,
        )

        # Resolve contact — use chat_id for DMs (sender_jid may be device JID for own messages)
        phone_jid = chat_id if chat_kind == "dm" else sender_jid
        phone_number = _jid_to_phone(phone_jid)
        if not phone_number:
            logger.warning(
                "unparseable phone from jid, dropping message: chat_id=%s sender_jid=%s",
                chat_id, sender_jid,
            )
            return
        contact_id = None
        is_trusted = False
        from server.services.channel_policies import WhatsAppInboundPolicy
        resolution = await WhatsAppInboundPolicy(self.ctx).resolve_sender(
            chat_kind=chat_kind, phone_number=phone_number, sender_name=sender_name)
        if not resolution.accepted:
            if resolution.drop_reason == "inbound_disabled":
                # Distinct log line so "why can't X reach me" is answerable
                # from journalctl.
                logger.warning(
                    "dropped DM: contact exists but inbound disabled: phone=%s contact=%s sender_name=%s preview=%r",
                    phone_number, resolution.contact_id, sender_name, text[:80],
                )
            else:
                logger.warning(
                    "dropped unknown whatsapp DM: phone=%s sender_jid=%s sender_name=%s preview=%r",
                    phone_number, sender_jid, sender_name, text[:80],
                )
            return
        contact_id = resolution.contact_id
        is_trusted = resolution.is_trusted

        # Derive session key. DM keys strip the WhatsApp linked-device
        # suffix (<phone>:<device>@s.whatsapp.net → <phone>): messages from
        # a companion device are the same human and must land in the same
        # conversation as the primary phone, not fork a :<device> variant.
        agent_id = "main"
        if chat_kind == "group":
            key_part = chat_id.split("@")[0] if "@" in chat_id else chat_id
        else:
            key_part = sender_jid.split("@")[0] if "@" in sender_jid else sender_jid
            key_part = key_part.split(":")[0]
        session_key = f"agent:{agent_id}:whatsapp:{chat_kind}:{key_part}"

        # Bob3 Phase VI item 3: canonicalize onto the conversation id. The
        # channel-derived key above is the binding; everything downstream
        # (participants, routes, messages, events, dispatch) keys under the
        # conversation, so a merged binding lands in its survivor
        # conversation with no per-call-site changes. 1:1 today (ensure()
        # backfills conversation.id = session_key) — diverges only on merge.
        binding_key = session_key
        try:
            from server.repositories.conversations import ConversationRepository
            conversation = await ConversationRepository(self.db).ensure(
                session_key,
                address=chat_id if chat_kind == "group" else phone_number,
                endpoint_kind=chat_kind)
            session_key = conversation["id"]
        except Exception:
            logger.warning("conversation resolve failed for %s", session_key, exc_info=True)

        # Slash command interception — trusted contacts only, never stored or dispatched
        if text.startswith("/"):
            logger.info("slash command intercepted from %s (trusted=%s): %s", sender_name, is_trusted, text[:50])
            if is_trusted:
                await self._handle_slash_command(text, session_key, chat_id, chat_kind, sender_jid, sender_name)
            return

        # Resolve @mentions: replace raw phone numbers with display names
        now_iso = utcnow().isoformat()
        if mentioned_jids and chat_kind == "group":
            mention_map: dict[str, str] = {}
            for jid in mentioned_jids:
                phone = _jid_to_phone(jid)
                if not phone:
                    continue
                # Try session participants first (group members with display names)
                from server.repositories.participants import ParticipantRepository
                participant = await ParticipantRepository(self.db).get(session_key, phone)
                if participant and participant["display_name"]:
                    mention_map[phone] = participant["display_name"]
                    continue
                # Then try contacts table
                from server.repositories.contacts import ContactRepository
                contact_match = await ContactRepository(self.db).get_by_phone(phone)
                if contact_match and contact_match["name"]:
                    mention_map[phone] = contact_match["name"]
                # Upsert mentioned user as participant so dispatch-time resolution can find them
                from server.repositories.participants import ParticipantRepository
                await ParticipantRepository(self.db).touch(
                    session_key, phone, mention_map.get(phone, phone), now_iso)
            # Replace @phone_number patterns with @DisplayName
            for phone, name in mention_map.items():
                bare = phone.lstrip("+")
                text = re.sub(rf"@{re.escape(bare)}\b", f"@{name}", text)

        # Upsert sender as session participant
        from server.repositories.participants import ParticipantRepository
        await ParticipantRepository(self.db).upsert(
            session_key, phone_number,
            display_name=sender_name or phone_number,
            contact_id=contact_id, is_trusted=bool(is_trusted), now_iso=now_iso)

        # Register the endpoint binding — DM carries contact_id, group the JID
        from server.repositories.conversations import ConversationRepository
        repo = ConversationRepository(self.db)
        if chat_kind == "group":
            await repo.register_endpoint(
                session_key, endpoint_kind="group", address=chat_id)
        else:
            await repo.register_endpoint(
                session_key, endpoint_kind="dm", address=chat_id,
                contact_id=str(contact_id) if contact_id else None)

        # Resolve agenda
        from server.services.session_agenda_service import SessionAgendaService
        agenda_svc = SessionAgendaService(self.ctx)
        agenda = await agenda_svc.get_effective_agenda(
            session_key, "whatsapp",
            contact_id=contact_id, is_trusted=is_trusted,
        )

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
                    from server.repositories.contacts import ContactRepository
                    repo = ContactRepository(self.db)
                    existing = await repo.get_by_phone(normalized_phone)
                    if not existing:
                        await repo.create(name=name, phone_number=normalized_phone)
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
        if agenda:
            user_content = agenda + "\n\n" + user_content
        if contacts_block:
            user_content += "\n\n" + contacts_block
        if document_workspace_path:
            user_content += (
                "\n\n## Document attached"
                f"\n- Filename: {document_filename}"
                f"\n- Saved at: `{document_workspace_path}` (relative to workspace)"
                f"\nUse `bash` to inspect it (e.g. `pdftotext {document_workspace_path} -`)."
            )
        user_content += "\n\nRespond to this message by calling send_whatsapp_message with your reply."

        # Store user message immediately so queued messages are visible
        # to the next dispatch that acquires the session lock.
        from server.services.session_service import SessionService
        message_metadata: dict[str, Any] | None = None
        if image_path:
            message_metadata = {
                "image_path": image_path,
                "image_mime_type": image_mime_type,
            }
        elif video_path:
            message_metadata = {
                "video_path": video_path,
                "video_mime_type": video_mime_type,
                "is_gif": is_gif,
            }
        elif document_path:
            message_metadata = {
                "document_path": document_path,
                "document_workspace_path": document_workspace_path,
                "document_filename": document_filename,
            }
        if image_path:
            fallback_text = "[Image]"
        elif video_path:
            fallback_text = "[GIF]" if is_gif else "[Video]"
        elif document_path:
            fallback_text = f"[Document: {document_filename}]"
        else:
            fallback_text = ""
        # Store the user message and append the ingress event in ONE
        # transaction (Bob3 invariants 1+2): the event log is the durable
        # record of accepted stimuli, keyed (source=whatsapp, external_id=
        # wa_message_id) so bridge redeliveries can't double-accept. Audit
        # -only in Phase I — dispatch still runs off session_messages.
        from server.repositories import Event, EventLogRepository

        event_repo = EventLogRepository(self.db)
        session_svc = SessionService(self.ctx)
        async with self.db.transaction() as txn:
            stored_msg_id = await session_svc.add_message(
                session_key, "user", text or fallback_text,
                channel="whatsapp", sender_id=contact_id, dispatched=0,
                metadata=message_metadata, txn=txn,
            )
            ingress_event_id = await event_repo.append(Event(
                event_type="message.received",
                binding_key=binding_key,
                conversation_id=session_key,
                source="whatsapp",
                external_id=wa_message_id or None,
                payload={
                    "session_message_id": stored_msg_id,
                    "chat_kind": chat_kind,
                    "sender_name": sender_name,
                    "contact_id": contact_id,
                    "has_media": bool(message_metadata),
                },
            ), txn=txn)

        logger.info("dispatching whatsapp message session=%s idempotency=%s", session_key, wa_message_id)

        dispatch_spec = await self._build_inbound_dispatch_spec(
            session_key=session_key,
            chat_id=chat_id,
            chat_kind=chat_kind,
            contact_id=contact_id,
            is_trusted=is_trusted,
            human_initiated=True,
            sender_name=sender_name,
            text_preview=text[:100],
        )

        async def _run_dispatch() -> str:
            from server.services.dispatch_runner import DispatchRunner
            return await DispatchRunner(self.ctx).run(dispatch_spec)

        # Attention coordinator (Bob3 Phase III cutover): Tier 0 addressed
        # detection + Tier 1 windows decide WHEN this dispatch runs; Tier 2
        # (probe_enabled) decides WHETHER an unaddressed group batch runs.
        # Route metadata keeps the legacy flag names for operator continuity.
        # Policy lives on the conversation (Increment 3).
        from server.repositories.conversations import ConversationRepository
        policy = await ConversationRepository(self.db).get_policy(session_key)
        probe_enabled = bool(policy.get("patience_enabled")) and bool(
            policy.get("patience_relevance_gating"))

        from server.services.attention import AttentionCoordinator

        # Occupancy (Bob3 Phase VI item 6): while a call is live on this
        # conversation, non-urgent text stays stored-but-undispatched and the
        # post-call drain (wake_session) runs it as one turn. Urgent text
        # dispatches immediately.
        from server.services import occupancy
        if occupancy.is_live(session_key) and not occupancy.is_urgent(text or ""):
            occupancy.defer(session_key)
            logger.info("occupancy: call live on %s — queued message for post-call turn",
                        session_key)
            return

        await AttentionCoordinator(self.ctx).submit(
            session_key, _run_dispatch,
            text=text or fallback_text,
            chat_kind=chat_kind,
            bot_name=settings.patience.bot_name,
            mentioned_jids=tuple(mentioned_jids or ()),
            sender_name=sender_name or "",
            is_trusted=is_trusted,
            probe_enabled=probe_enabled,
            probe_model=settings.patience.model or settings.openai.get_memory_model(),
            event_id=ingress_event_id,
        )

        # Auto-subscribe to presence for this chat so typing indicators can
        # extend the attention window.
        if chat_id not in self._presence_subscribed:
            await self.subscribe_presence(chat_id)
            self._presence_subscribed.add(chat_id)

    async def _build_inbound_dispatch_spec(
        self,
        *,
        session_key: str,
        chat_id: str,
        chat_kind: str,
        contact_id: str | None,
        is_trusted: bool,
        human_initiated: bool,
        sender_name: str = "",
        text_preview: str = "",
    ) -> "DispatchSpec":
        """Assemble the full inbound-WhatsApp DispatchSpec (system prompt,
        tools, send tool, quota handling). Shared by the live inbound path
        (human_initiated=True), the wake path and the crash-recovery sweep
        (`wake_session`, which derives the flag from the undispatched rows).

        human_initiated splits the cross-conversation messaging tools so the
        model can't pick wrong: human-started turns get steer_conversation
        only, autonomous wake turns get send_whatsapp_group_message only."""
        from server.services.context_assembler import ContextAssembler
        from server.services.prompt_assembler import load_workspace_prompt

        settings = self._get_settings()
        assembler = ContextAssembler(self.ctx)
        workspace_prompt = await load_workspace_prompt(settings.harness.workspace_dir, db=self.db)
        participants_prompt = await assembler.participants_prompt(session_key)

        person_context = ""
        if chat_kind != "group":
            person_context = await assembler.person_profile(contact_id)

        group_memory_hint = ""
        if chat_kind == "group":
            group_memory_hint = await assembler.group_memory_hint(session_key)

        # Dream plans — Tier 1 injection for sessions with linked plans
        dream_plans_prompt = await assembler.dream_plans_prompt(session_key)

        # Active goals held by this conversation (Bob Events §1.4) — includes
        # the old outreach block; outreach state rides in the goal itself.
        goals_prompt = await assembler.goals_block(session_key)

        # The turn-start clock is appended by build_chat_messages (2026-09-01
        # fan-out to every channel) — date-relative reasoning ("tomorrow",
        # "this week") and local-vs-UTC arithmetic need a grounded now.
        system_content = "\n\n".join(
            p for p in (workspace_prompt, participants_prompt, person_context, group_memory_hint, dream_plans_prompt, goals_prompt) if p
        )

        from server.services.llm_dispatch import LLMDispatchService
        from server.services.tools import Tool
        from server.services.tool_registry import build_common_tools
        from server.services.group_tools import make_group_tools

        wa_service = self

        # Core tools (workspace, memory, docs, changelog, email_send, contact, phone, reflection, delegation)
        tools = build_common_tools(self.ctx, session_key=session_key, is_trusted=is_trusted, contact_id=contact_id)

        # Group-specific tools
        if chat_kind == "group":
            tools.extend(make_group_tools(self.ctx, session_key=session_key))

        # WhatsApp-specific: outreach tools (trusted DMs and groups)
        if contact_id and (is_trusted or chat_kind == "group"):
            from server.services.whatsapp_outreach_tools import make_whatsapp_outreach_tools
            tools.extend(make_whatsapp_outreach_tools(self.ctx, self, session_key))

        # Bob Events §1.5: proactive group send — autonomous wake-path turns
        # only (goal deadlines, reviser wakes, nudges), trusted only; each
        # target group must also enable it via conversation policy (off by
        # default). Human-started turns get steer_conversation instead:
        # attaching both let the model route user-requested messaging through
        # the verbatim direct send (2026-08-30 AI-doom double-post), so the
        # split is structural now.
        if is_trusted and not human_initiated:
            from server.services.whatsapp_outreach_tools import make_group_send_tools
            tools.extend(make_group_send_tools(self.ctx, self, session_key))

        # Steering (docs/steering-plan.md): any turn a human contact started
        # can request a steer — owner requests fire directly, everyone
        # else's routes through owner approval. Wake-path turns never steer
        # (no nested steering): they may carry the route's contact id, but
        # no human dispatched them, so the gate is the dispatch origin, not
        # contact resolution.
        if contact_id and human_initiated:
            from server.services.steering import make_steering_tools
            tools.extend(make_steering_tools(self.ctx, session_key, contact_id))

        # Goal tools (Bob3 Phase V): trusted sessions can create/track goals.
        if is_trusted:
            from server.services.goal_tools import make_goal_tools
            tools.extend(make_goal_tools(self.ctx, session_key))
            # Bob Events §3.4: the payment gate's human side.
            from server.services.approval_tools import make_approval_tools
            tools.extend(make_approval_tools(self.ctx, session_key))

        # Voice outreach: attach whenever the requester is a trusted contact, in any
        # chat context (DM or group). Untrusted users don't get the tool — it costs
        # real money (Twilio) and can ping arbitrary contacts, so it's gated on
        # trust rather than chat kind. `initiate_voice_call` self-gates to DM-only
        # via its chat-id check, so attaching it in groups is harmless.
        if is_trusted:
            from server.services.voice_outreach_tools import make_voice_outreach_tools
            tools.extend(make_voice_outreach_tools(self.ctx, self, session_key))

        # Outreach reply tool for active outreach targets (goal-backed).
        from server.repositories.goals import GoalRepository
        active_outreach = await GoalRepository(self.db).active_outreach(session_key)
        if active_outreach:
            from server.services.whatsapp_outreach_tools import make_outreach_reply_tools
            tools.extend(make_outreach_reply_tools(self.ctx, self, session_key))

        message_was_sent = [False]
        sent_texts: list[str] = []
        send_seq = [0]
        # Backburner capture mode (docs/backburner-plan.md D2): flipped by the
        # detach sequence after this turn goes to the background — the send
        # tool then captures replies as the task result instead of delivering.
        backburner_capture: dict[str, Any] = {"enabled": False, "texts": []}

        async def _send_whatsapp_message(text: str, media_path: str = "") -> str:
            # Leaked-markup guard (2026-09-04 Bob-management leak): models
            # sometimes emit their send call as <tool_call> XML text, and
            # upstream parsing can eat the opening half leaving tag soup.
            # Never post that raw — strip the markup; a reply that is ONLY
            # markup is refused so the model restates it as plain text.
            from server.services.openai_service import strip_leaked_tool_xml

            cleaned = strip_leaked_tool_xml(text)
            if cleaned.strip() or media_path:
                text = cleaned
            else:
                return ("Error: not sent — the reply contained only leaked "
                        "tool-call markup. Restate it as a plain message and "
                        "call this tool again with the text.")
            # Detached turn: capture, don't deliver. The supervisor relays the
            # result to the user via the wake path once the task finishes.
            if backburner_capture["enabled"] and not is_no_reply(text):
                backburner_capture["texts"].append(text)
                return ("This turn was detached — your reply was captured and will be "
                        "relayed to the user when this background task finishes. Do not "
                        "attempt other send routes.")
            # Bob3 Phase IV: sends go through the effects outbox — recorded
            # durably, delivered inline, retried by the pump after a crash.
            # History (sent_texts) is written from delivery confirmation.
            from server.services.effects import emit_and_deliver

            message_was_sent[0] = True
            if is_no_reply(text):
                return "No reply sent."
            text = strip_citation_markers(text)
            seq = send_seq[0]
            send_seq[0] += 1
            if media_path:
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

        async def _send_holding_ack(text: str) -> None:
            """Backburner holding ack — sent by the detach sequence while the
            turn's own send tool is still mid-flight. Own idempotency key so a
            retry can never duplicate a turn-send."""
            from server.services.effects import emit_and_deliver

            result = await emit_and_deliver(
                self.ctx, kind="whatsapp_send",
                idempotency_key=f"whatsapp_send:hold:{dispatch_id}",
                payload={"chat_id": chat_id, "text": strip_citation_markers(text)})
            if not result.get("ok"):
                logger.warning("backburner: holding ack delivery failed (dispatch=%s): %s",
                               dispatch_id, result.get("error"))

        from server.services.dispatch_runner import DispatchRunner, DispatchSpec

        async def _on_quota_exhausted() -> None:
            await _notify_quota_exhausted(wa_service, chat_id, session_key)

        # Typing indicator bracket: active when the turn's LLM phase begins,
        # settled on every turn exit (the runner's finally). Covers inbound,
        # wake/steering/approval, occupancy drain, crash recovery, and
        # backburner relays — every path through this spec builder.
        async def _on_turn_active() -> None:
            await self.start_typing(chat_id)

        async def _on_turn_settled() -> None:
            await self.stop_typing(chat_id)

        dispatch_spec = DispatchSpec(
            session_key=session_key,
            system_content=system_content,
            tools=tools,
            call_category="whatsapp_incoming",
            send_tool_name="send_whatsapp_message",
            dispatch_id=dispatch_id,
            contact_id=contact_id,
            channel="whatsapp",
            max_history=100,
            history_policy="delivered_only",
            message_was_sent=message_was_sent,
            sent_texts=sent_texts,
            backburner_capture=backburner_capture,
            hold_sender=_send_holding_ack,
            quota_restore=True,
            on_quota_exhausted=_on_quota_exhausted,
            on_turn_active=_on_turn_active,
            on_turn_settled=_on_turn_settled,
            event=("whatsapp.message.received", {
                "session_key": session_key,
                "sender_name": sender_name,
                "chat_kind": chat_kind,
                "text_preview": text_preview,
            }),
        )

        return dispatch_spec

    async def resume_pending_sessions(self) -> int:
        """Crash-recovery sweep (Bob3 Phase III item 5): re-arm dispatch for
        WhatsApp sessions holding stored-but-undispatched user messages, so a
        kill -9 during an armed attention window loses zero messages.

        Returns the number of sessions re-armed."""
        from server.repositories.history import HistoryRepository
        rows = await HistoryRepository(self.db).undispatched_conversations(
            channel="whatsapp")
        resumed = 0
        for session_key in rows:
            try:
                await self.wake_session(session_key)
                resumed += 1
                logger.info("recovery: re-armed dispatch for %s", session_key)
            except Exception:
                logger.warning("recovery: failed to resume %s", session_key, exc_info=True)
        return resumed

    async def wake_session(self, session_key: str) -> None:
        """Arm a dispatch for a session with stored-but-undispatched messages.

        Shared by the crash-recovery sweep and the Bob3 wake path (goal
        completions, subagent results, deadline wakeups): the caller stores
        an undispatched user message, then this runs a turn through the full
        inbound pipeline (attention coordinator, turn claims, effect sends).

        The turn counts as human-initiated when any undispatched row is a
        raw inbound message (no provenance label) — e.g. post-call occupancy
        drain of queued human texts, or crash recovery of a never-dispatched
        inbound. Pure wake-content rows keep the turn autonomous.
        """
        from server.repositories.conversations import (
            ConversationRepository, wa_send_jid)
        from server.repositories.history import HistoryRepository
        route = await ConversationRepository(self.db).route_for(session_key)
        jid = wa_send_jid(route["address"]) if route and route["is_active"] else None
        if not jid:
            raise RuntimeError(f"no active route for session {session_key}")
        chat_kind = "group" if ":group:" in session_key else "dm"
        contact_id = route["contact_id"]
        is_trusted = False
        if contact_id:
            from server.repositories.contacts import ContactRepository
            trusted = await ContactRepository(self.db).is_trusted(contact_id)
            is_trusted = bool(trusted)

        spec = await self._build_inbound_dispatch_spec(
            session_key=session_key,
            chat_id=jid,
            chat_kind=chat_kind,
            contact_id=contact_id,
            is_trusted=is_trusted,
            human_initiated=await HistoryRepository(self.db).has_undispatched_inbound(
                session_key),
        )

        from server.services.attention import AttentionCoordinator
        from server.services.dispatch_runner import DispatchRunner

        async def _run_dispatch(s=spec) -> str:
            return await DispatchRunner(self.ctx).run(s)

        await AttentionCoordinator(self.ctx).resume_pending(session_key, _run_dispatch)
