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

from bob_server.config import Settings
from bob_server.context import AppContext
from bob_server.models import SessionRouteCreate, SessionRouteKind
from bob_server.services.base import BaseService, utcnow
from bob_server.services.openai_service import strip_citation_markers
from bob_server.services.session_route_service import SessionRouteService
from bob_server.services.whatsapp_bridge_service._media import _jid_to_phone, _prepare_media

logger = logging.getLogger(__name__)

# Rate limiting for quota-exhaustion notifications: session_key -> monotonic
# timestamp of the last notification sent. Resets on process restart, which is
# fine — if Bob restarts, credit may have been topped up in the meantime.
_quota_notify_last: dict[str, float] = {}
_QUOTA_NOTIFY_MIN_INTERVAL = 3600.0  # 1 hour


def _is_quota_error(exc: Exception) -> bool:
    """True if the exception looks like an OpenAI insufficient-quota failure.

    openai_service wraps the SDK's RateLimitError in a RuntimeError, so we
    detect by message rather than by type.
    """
    msg = str(exc).lower()
    return "insufficient_quota" in msg or ("429" in msg and "quota" in msg)


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




from bob_server.services.whatsapp_bridge_service._group_events import GroupEventsMixin
from bob_server.services.whatsapp_bridge_service._slash_commands import SlashCommandsMixin


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
        self._subagent_queue: asyncio.Queue[dict[str, Any]] | None = None
        self._subagent_listener_task: asyncio.Task | None = None
        self._verbose_queue: asyncio.Queue[dict[str, Any]] | None = None
        self._verbose_listener_task: asyncio.Task | None = None
        self._presence_subscribed: set[str] = set()

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        settings = self._get_settings()
        if not settings.whatsapp_bridge.enabled:
            return
        self._task = asyncio.create_task(self._run_loop(), name="whatsapp_bridge")

        # Subscribe to subagent result events and trigger dispatches
        if self.ctx.event_bus:
            self._subagent_queue = self.ctx.event_bus.subscribe()
            self._subagent_listener_task = asyncio.create_task(
                self._subagent_event_loop(), name="subagent_listener"
            )
            # Subscribe to memory verbose notices and forward to WhatsApp.
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
        if self._subagent_listener_task is not None:
            self._subagent_listener_task.cancel()
            try:
                await self._subagent_listener_task
            except asyncio.CancelledError:
                pass
            self._subagent_listener_task = None
        if self._subagent_queue is not None and self.ctx.event_bus:
            self.ctx.event_bus.unsubscribe(self._subagent_queue)
            self._subagent_queue = None
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

    async def _subagent_event_loop(self) -> None:
        """Listen for subagent.result_ready events and trigger dispatches."""
        assert self._subagent_queue is not None
        try:
            while True:
                event = await self._subagent_queue.get()
                event_type = event.get("type", "")
                if event_type != "subagent.result_ready":
                    continue
                payload = event.get("payload", {})
                parent_session_key = payload.get("parent_session_key", "")
                if ":whatsapp:" not in parent_session_key:
                    continue
                try:
                    await self._dispatch_subagent_result(parent_session_key)
                except Exception:
                    logger.exception("failed to dispatch subagent result for %s", parent_session_key)
        except asyncio.CancelledError:
            pass

    async def _verbose_event_loop(self) -> None:
        """Listen for memory.verbose_notice events and forward to WhatsApp.

        Filters to WhatsApp-routed sessions; resolves the chat_id via
        session_routes and calls send_message. Silently drops events for
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
                    route = await self.db.fetch_one(
                        "SELECT chat_id FROM session_routes "
                        "WHERE session_key = ? AND deleted_at IS NULL AND is_active = 1",
                        (session_key,),
                    )
                    if not route or not route["chat_id"]:
                        continue
                    await self.send_message(route["chat_id"], text)
                except Exception:
                    logger.exception("failed to forward verbose notice for %s", session_key)
        except asyncio.CancelledError:
            pass

    async def _dispatch_subagent_result(self, session_key: str) -> None:
        """Dispatch a subagent result into the parent WhatsApp session."""
        settings = self._get_settings()
        if not settings.openai.enabled:
            return

        # Resolve context from session route
        route = await self.db.fetch_one(
            "SELECT channel, kind, contact_id, chat_id, metadata FROM session_routes WHERE session_key = ?",
            (session_key,),
        )
        if not route or route["channel"] != "whatsapp":
            return

        chat_id = route["chat_id"]
        contact_id = route["contact_id"]
        is_trusted = False
        if contact_id:
            contact = await self.db.fetch_one(
                "SELECT is_trusted FROM contacts WHERE id = ? AND deleted_at IS NULL",
                (contact_id,),
            )
            if contact:
                is_trusted = bool(contact.get("is_trusted", 0))

        # Build system prompt
        from bob_server.services.session_agenda_service import SessionAgendaService
        from bob_server.services.prompt_assembler import load_workspace_prompt, build_chat_messages

        agenda_svc = SessionAgendaService(self.ctx)
        agenda = await agenda_svc.get_effective_agenda(
            session_key, "whatsapp",
            contact_id=contact_id, is_trusted=is_trusted,
        )
        workspace_prompt = await load_workspace_prompt(settings.harness.workspace_dir, db=self.db)
        participants_prompt = await self._build_participants_prompt(session_key)

        system_content = "\n\n".join(
            p for p in (workspace_prompt, participants_prompt, agenda) if p
        )

        # Build tools
        from bob_server.services.llm_dispatch import LLMDispatchService
        from bob_server.services.tools import Tool
        from bob_server.services.tool_registry import build_common_tools
        from bob_server.services.group_tools import make_group_tools

        tools = build_common_tools(self.ctx, session_key=session_key, is_trusted=is_trusted, contact_id=contact_id)

        # Add group tools if this is a group session
        route_for_kind = await self.db.fetch_one(
            "SELECT kind FROM session_routes WHERE session_key = ?",
            (session_key,),
        )
        if route_for_kind and route_for_kind["kind"] == "group":
            tools.extend(make_group_tools(self.ctx, session_key=session_key))

        wa_service = self
        message_was_sent = [False]
        sent_texts: list[str] = []

        async def _send_whatsapp_message(text: str) -> str:
            message_was_sent[0] = True
            if text.strip().upper() == "NO_REPLY":
                return "No reply sent."
            text = strip_citation_markers(text)
            sent_texts.append(text)
            request_id = await wa_service.send_message(chat_id, text)
            return f"Message sent (request_id={request_id})"

        tools.append(Tool(
            name="send_whatsapp_message",
            description=(
                "Send a reply to the current WhatsApp conversation. "
                "You MUST call this tool to deliver your response."
            ),
            parameters={"text": {"type": "string", "description": "The message text to send."}},
            required=["text"],
            handler=_send_whatsapp_message,
        ))

        dispatch_id = str(uuid4())

        async def _run_dispatch() -> str:
            from bob_server.services.session_service import SessionService
            from bob_server.services.session_dispatch_gate import SessionDispatchGate

            session_svc = SessionService(self.ctx)
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

                result = await LLMDispatchService(self.ctx).chat_with_tools(
                    messages, tools,
                    call_category="subagent_result",
                    session_key=session_key,
                    dispatch_id=dispatch_id,
                    contact_id=contact_id,
                )
                if not message_was_sent[0] and result.strip():
                    from bob_server.services.tap import tap_dispatch, tap_enabled
                    if tap_enabled():
                        result = await tap_dispatch(
                            self.ctx, messages=messages, tools=tools,
                            session_key=session_key,
                            send_tool_name="send_whatsapp_message",
                            first_result=result,
                            call_category="subagent_result",
                            dispatch_id=dispatch_id,
                            contact_id=contact_id,
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

    async def _build_participants_prompt(self, session_key: str) -> str:
        # For group sessions, use the group members table for richer info
        if ":group:" in session_key:
            route = await self.db.fetch_one(
                "SELECT chat_id FROM session_routes WHERE session_key = ?",
                (session_key,),
            )
            if route and route["chat_id"]:
                group = await self.db.fetch_one(
                    "SELECT id, name, member_count FROM whatsappgroups WHERE whatsapp_jid = ? AND deleted_at IS NULL",
                    (route["chat_id"],),
                )
                if group:
                    members = await self.db.fetch_all(
                        """SELECT gm.display_name, gm.is_admin, gm.is_super_admin,
                                  c.name as contact_name, c.is_trusted
                           FROM whatsappgroup_members gm
                           JOIN contacts c ON c.id = gm.contact_id AND c.deleted_at IS NULL
                           WHERE gm.group_id = ? AND gm.left_at IS NULL
                           ORDER BY gm.is_super_admin DESC, gm.is_admin DESC, gm.display_name ASC""",
                        (group["id"],),
                    )
                    if members:
                        lines = [f"## Participants ({len(members)} members in {group['name'] or 'group'})"]
                        for m in members:
                            name = m["display_name"] or m["contact_name"] or "Unknown"
                            badges = []
                            if m["is_super_admin"]:
                                badges.append("super admin")
                            elif m["is_admin"]:
                                badges.append("admin")
                            trust = "trusted" if m["is_trusted"] else "untrusted"
                            badges.append(trust)
                            lines.append(f"- {name} ({', '.join(badges)})")
                        return "\n".join(lines)

        # Fallback: session_participants for DMs or when group data unavailable
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

    async def _lookup_contact(self, phone_number: str) -> tuple[str, bool] | None:
        """Return (contact_id, is_trusted) for an existing contact, or None.

        Exact match first, then prefix-match fallback to catch WhatsApp JIDs with
        extra trailing digits (e.g. +614154068544 should match existing +61415406854).
        """
        contact = await self.db.fetch_one(
            "SELECT id, is_trusted FROM contacts WHERE phone_number = ? AND deleted_at IS NULL LIMIT 1",
            (phone_number,),
        )
        if contact:
            return contact["id"], bool(contact.get("is_trusted", 0))

        if len(phone_number) > 6:
            prefix_matches = await self.db.fetch_all(
                "SELECT id, is_trusted, phone_number FROM contacts WHERE deleted_at IS NULL "
                "AND (phone_number = ? OR ? LIKE phone_number || '%' OR phone_number LIKE ? || '%') "
                "ORDER BY LENGTH(phone_number) DESC LIMIT 1",
                (phone_number[:-1], phone_number, phone_number),
            )
            if prefix_matches:
                best = prefix_matches[0]
                logger.info("resolved contact %s via prefix match: %s → %s", best["id"], phone_number, best["phone_number"])
                return best["id"], bool(best.get("is_trusted", 0))
        return None

    async def _resolve_or_seed_contact(self, phone_number: str, display_name: str = "") -> tuple[str, bool]:
        """Find an existing contact by phone or auto-seed an untrusted one. Returns (contact_id, is_trusted)."""
        existing = await self._lookup_contact(phone_number)
        if existing:
            return existing
        new_id = str(uuid4())
        now_iso = utcnow().isoformat()
        await self.db.execute(
            """INSERT INTO contacts (id, name, phone_number, is_trusted, created_at, updated_at)
               VALUES (?, ?, ?, 0, ?, ?)""",
            (new_id, display_name or phone_number, phone_number, now_iso, now_iso),
        )
        logger.info("auto-seeded untrusted contact %s for phone %s", new_id, phone_number)

        # Auto-create person memory entry
        from bob_server.services.memory import MemoryService
        mem_svc = MemoryService(self.ctx)
        await mem_svc.ensure_person_entry(
            self.ctx.settings.harness.workspace_dir,
            contact_id=new_id, name=display_name or phone_number,
            phone_number=phone_number, channel="WhatsApp",
        )

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
            key_part = sender_jid.split("@")[0]
        session_key = f"agent:{agent_id}:whatsapp:{chat_kind}:{key_part}"

        # Check if patience is enabled for this session
        route_row = await self.db.fetch_one(
            "SELECT metadata FROM session_routes WHERE session_key = ? AND deleted_at IS NULL AND is_active = 1",
            (session_key,),
        )
        if not route_row or not route_row["metadata"]:
            return
        try:
            route_meta = json.loads(route_row["metadata"])
        except (json.JSONDecodeError, TypeError):
            return
        if not route_meta.get("patience_enabled"):
            return

        import time as _time
        from bob_server.services.patience_buffer import PendingItem, PatienceBufferRegistry

        item = PendingItem(
            item_type="typing",
            timestamp=_time.monotonic(),
            sender_jid=sender_jid,
            sender_name=sender_name or "",
            payload={},
        )
        buffer = PatienceBufferRegistry.get(session_key)

        # Keep only the latest typing event per sender to avoid buffer bloat
        buffer.items = [i for i in buffer.items if i.item_type != "typing" or i.sender_jid != sender_jid]
        buffer.add(item)

        logger.info("patience: typing indicator from %s in %s, buffer=%d messages + %d typing",
                     sender_name, session_key,
                     len([i for i in buffer.items if i.item_type == "message"]),
                     len([i for i in buffer.items if i.item_type == "typing"]))


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
        contact_id = None
        is_trusted = False
        contact = await self.db.fetch_one(
            "SELECT id, name, is_trusted FROM contacts WHERE phone_number = ? AND deleted_at IS NULL LIMIT 1",
            (phone_number,),
        )
        if contact:
            contact_id = contact["id"]
            is_trusted = bool(contact.get("is_trusted", 0))
            logger.info("resolved contact %s (trusted=%s) for phone %s", contact_id, is_trusted, phone_number)
            # Backfill name from WhatsApp if contact has no real name
            if sender_name and contact["name"] in ("", phone_number):
                await self.db.execute(
                    "UPDATE contacts SET name = ?, updated_at = ? WHERE id = ?",
                    (sender_name, utcnow().isoformat(), contact_id),
                )
                from bob_server.services.memory import MemoryService
                await MemoryService(self.ctx).sync_person_display_name_for_contact(
                    contact_id, sender_name,
                )
        elif chat_kind == "dm":
            # Security gate: drop DMs from numbers with no contact row.
            # Group sync auto-seeds contacts for everyone Bob has seen in a group,
            # so any legitimate acquaintance already has a row. Pure unknowns get
            # logged and never become a session.
            logger.warning(
                "dropped unknown whatsapp DM: phone=%s sender_jid=%s sender_name=%s preview=%r",
                phone_number, sender_jid, sender_name, text[:80],
            )
            return
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

            # Auto-create person memory entry for new contacts
            from bob_server.services.memory import MemoryService
            mem_svc = MemoryService(self.ctx)
            await mem_svc.ensure_person_entry(
                settings.harness.workspace_dir,
                contact_id=contact_id, name=sender_name or phone_number,
                phone_number=phone_number, channel="WhatsApp",
            )

        # Derive session key
        agent_id = "main"
        if chat_kind == "group":
            key_part = chat_id.split("@")[0] if "@" in chat_id else chat_id
        else:
            key_part = sender_jid.split("@")[0] if "@" in sender_jid else sender_jid
        session_key = f"agent:{agent_id}:whatsapp:{chat_kind}:{key_part}"

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
                # Try session participants first (group members with display names)
                participant = await self.db.fetch_one(
                    "SELECT display_name FROM session_participants WHERE identifier = ? AND session_key = ? LIMIT 1",
                    (phone, session_key),
                )
                if participant and participant["display_name"]:
                    mention_map[phone] = participant["display_name"]
                    continue
                # Then try contacts table
                contact_match = await self.db.fetch_one(
                    "SELECT name FROM contacts WHERE phone_number = ? AND deleted_at IS NULL LIMIT 1",
                    (phone,),
                )
                if contact_match and contact_match["name"]:
                    mention_map[phone] = contact_match["name"]
                # Upsert mentioned user as participant so dispatch-time resolution can find them
                await self.db.execute(
                    """INSERT INTO session_participants (session_key, identifier, display_name, contact_id, is_trusted, last_active_at)
                       VALUES (?, ?, ?, ?, 0, ?)
                       ON CONFLICT(session_key, identifier) DO UPDATE SET
                           last_active_at = excluded.last_active_at""",
                    (session_key, phone, mention_map.get(phone, phone), None, now_iso),
                )
            # Replace @phone_number patterns with @DisplayName
            for phone, name in mention_map.items():
                bare = phone.lstrip("+")
                text = re.sub(rf"@{re.escape(bare)}\b", f"@{name}", text)

        # Upsert sender as session participant
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
        from bob_server.exceptions import ConflictError
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
                    chat_id=chat_id,
                    metadata={
                        "sender_jid": sender_jid,
                        "sender_name": sender_name,
                    },
                ))
        except ConflictError:
            pass  # Route already exists, proceed with dispatch

        # Resolve agenda
        from bob_server.services.session_agenda_service import SessionAgendaService
        agenda_svc = SessionAgendaService(self.ctx)
        agenda = await agenda_svc.get_effective_agenda(
            session_key, "whatsapp",
            contact_id=contact_id, is_trusted=is_trusted,
        )

        # Build system prompt: workspace context + agenda + participants
        from bob_server.services.prompt_assembler import load_workspace_prompt, build_chat_messages
        workspace_prompt = await load_workspace_prompt(settings.harness.workspace_dir, db=self.db)

        participants_prompt = await self._build_participants_prompt(session_key)

        # Inject person profile for DM sessions
        person_context = ""
        if contact_id and chat_kind != "group":
            from bob_server.services.memory import MemoryService
            mem_svc = MemoryService(self.ctx)
            entry = await mem_svc.find_person_entry(
                settings.harness.workspace_dir, contact_id=contact_id,
            )
            if entry:
                person_context = f"## Person Profile\n\n{entry}"

        # Inject group entity hint for group sessions
        group_memory_hint = ""
        if chat_kind == "group":
            group_row = await self.db.fetch_one(
                "SELECT wg.memory_entity_id FROM whatsappgroups wg "
                "JOIN session_routes sr ON sr.chat_id = wg.whatsapp_jid "
                "WHERE sr.session_key = ? AND wg.deleted_at IS NULL",
                (session_key,),
            )
            if group_row and group_row["memory_entity_id"]:
                eid = group_row["memory_entity_id"]
                group_memory_hint = (
                    "## Group Memory\n\n"
                    f"This is a WhatsApp group with accumulated memory entity `{eid}`.\n"
                    f"Use `recall('{eid}')` to look up group knowledge."
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
        from bob_server.services.session_service import SessionService
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
        await SessionService(self.ctx).add_message(
            session_key, "user", text or fallback_text,
            channel="whatsapp", sender_id=contact_id, dispatched=0,
            metadata=message_metadata,
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

        system_content = "\n\n".join(
            p for p in (workspace_prompt, participants_prompt, person_context, group_memory_hint, outreach_prompt) if p
        )

        logger.info("dispatching whatsapp message session=%s idempotency=%s", session_key, wa_message_id)

        from bob_server.services.llm_dispatch import LLMDispatchService
        from bob_server.services.tools import Tool
        from bob_server.services.tool_registry import build_common_tools
        from bob_server.services.group_tools import make_group_tools

        wa_service = self

        # Core tools (workspace, memory, docs, changelog, email_send, contact, phone, reflection, delegation)
        tools = build_common_tools(self.ctx, session_key=session_key, is_trusted=is_trusted, contact_id=contact_id)

        # Group-specific tools
        if chat_kind == "group":
            tools.extend(make_group_tools(self.ctx, session_key=session_key))

        # WhatsApp-specific: outreach tools (trusted DMs and groups)
        if contact_id and (is_trusted or chat_kind == "group"):
            from bob_server.services.whatsapp_outreach_tools import make_whatsapp_outreach_tools
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
                from bob_server.services.whatsapp_outreach_tools import make_outreach_reply_tools
                tools.extend(make_outreach_reply_tools(self.ctx, self, session_key))

        message_was_sent = [False]
        sent_texts: list[str] = []

        async def _send_whatsapp_message(text: str, media_path: str = "") -> str:
            message_was_sent[0] = True
            if text.strip().upper() == "NO_REPLY":
                return "No reply sent."
            text = strip_citation_markers(text)
            if media_path:
                workspace = settings.harness.workspace_dir.expanduser().resolve()
                resolved = (workspace / media_path).resolve()
                if not str(resolved).startswith(str(workspace)):
                    return "Error: path escapes workspace"
                if not resolved.is_file():
                    return f"Error: file not found: {media_path}"
                prepared = await _prepare_media(str(resolved))
                if prepared is None:
                    return "Error: failed to prepare media for sending"
                sent_texts.append(f"[Image: {text}]" if text else f"[Image: {resolved.name}]")
                request_id = await wa_service.send_media(chat_id, prepared, caption=text)
                return f"Media sent (request_id={request_id})"
            sent_texts.append(text)
            request_id = await wa_service.send_message(chat_id, text)
            return f"Message sent (request_id={request_id})"

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

        async def _run_dispatch() -> str:
            from bob_server.services.session_service import SessionService
            from bob_server.services.session_dispatch_gate import SessionDispatchGate

            session_svc = SessionService(self.ctx)
            async with SessionDispatchGate.get_lock(session_key):
                # Capture IDs of the messages we're about to claim, so we can
                # restore them if the LLM call fails due to quota exhaustion.
                # Without this, mark_dispatched silently swallows messages that
                # never got a reply.
                pre_rows = await self.db.fetch_all(
                    "SELECT id FROM session_messages "
                    "WHERE session_key = ? AND role = 'user' AND dispatched = 0",
                    (session_key,),
                )
                claimed_ids = [r["id"] for r in pre_rows]
                if not claimed_ids:
                    return ""
                await session_svc.mark_dispatched(session_key)

                messages = await build_chat_messages(
                    None, session_key,
                    db=self.db,
                    system_content=system_content,
                    max_history=100,
                )

                try:
                    result = await LLMDispatchService(self.ctx).chat_with_tools(
                        messages, tools,
                        call_category="whatsapp_incoming",
                        session_key=session_key,
                        dispatch_id=dispatch_id,
                        contact_id=contact_id,
                    )
                except Exception as exc:
                    if _is_quota_error(exc):
                        placeholders = ",".join("?" for _ in claimed_ids)
                        await self.db.execute(
                            f"UPDATE session_messages SET dispatched = 0 "
                            f"WHERE id IN ({placeholders})",
                            tuple(claimed_ids),
                        )
                        logger.warning(
                            "quota exhausted for %s; restored %d message(s) for retry",
                            session_key, len(claimed_ids),
                        )
                        await _notify_quota_exhausted(wa_service, chat_id, session_key)
                        return ""
                    raise
                # Tap: if LLM produced text but didn't use send_whatsapp_message,
                # give it a second chance with a reminder.
                if not message_was_sent[0] and result.strip():
                    from bob_server.services.tap import tap_dispatch, tap_enabled
                    if tap_enabled():
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
                    await session_svc.add_message(session_key, "assistant", assistant_text, channel="whatsapp", dispatch_id=dispatch_id)
                if self.ctx.event_bus:
                    await self.ctx.event_bus.publish("whatsapp.message.received", {
                        "session_key": session_key,
                        "sender_name": sender_name,
                        "chat_kind": chat_kind,
                        "text_preview": text[:100],
                    })
                return result

        # Check if patience is enabled for this session (per-session via route metadata)
        patience_enabled = False
        relevance_gating_enabled = False
        route_row = await self.db.fetch_one(
            "SELECT metadata FROM session_routes WHERE session_key = ? AND deleted_at IS NULL AND is_active = 1",
            (session_key,),
        )
        if route_row and route_row["metadata"]:
            try:
                route_meta = json.loads(route_row["metadata"])
                patience_enabled = route_meta.get("patience_enabled", False)
                relevance_gating_enabled = route_meta.get("patience_relevance_gating", False)
            except (json.JSONDecodeError, TypeError):
                pass
        logger.info(
            "patience check: session=%s route_found=%s enabled=%s relevance=%s",
            session_key, route_row is not None, patience_enabled, relevance_gating_enabled,
        )

        # Always route through the buffer. Patience-on runs the LLM gate on top;
        # patience-off uses a fixed settle delay to absorb bursts.
        import time as _time
        from bob_server.services.patience_buffer import PendingItem
        from bob_server.services.patience_gate import submit_to_patience

        item = PendingItem(
            item_type="message",
            timestamp=_time.monotonic(),
            sender_jid=sender_jid,
            sender_name=sender_name or "",
            payload={"text": text},
        )
        await submit_to_patience(
            self.ctx, session_key, item, _run_dispatch,
            bot_name=settings.patience.bot_name,
            model=settings.patience.model,
            max_pending_items=settings.patience.max_pending_items,
            max_context_messages=settings.patience.max_context_messages,
            relevance_gating_enabled=relevance_gating_enabled,
            patience_enabled=patience_enabled,
            settle_seconds=settings.patience.patience_off_settle_seconds,
        )

        # Auto-subscribe to presence for this chat (only meaningful when patience is on,
        # since typing-indicator extension is a patience-on feature).
        if patience_enabled and chat_id not in self._presence_subscribed:
            await self.subscribe_presence(chat_id)
            self._presence_subscribed.add(chat_id)
