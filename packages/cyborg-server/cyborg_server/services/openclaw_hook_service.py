"""Direct OpenClaw gateway delivery for Cyborg notifications."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
import logging
from pathlib import Path
import time
from typing import Any, Coroutine
from uuid import uuid4

from cyborg_server import __version__
from cyborg_server.config import OpenClawHookSettings, Settings
from cyborg_server.context import AppContext
from cyborg_server.database import Database
from cyborg_server.models import NotificationDeliveryStatus, NotificationType
from cyborg_server.services.base import BaseService, utcnow
from cyborg_server.services.prompt_history import log_prompt
from cyborg_server.services.session_route_service import SessionRouteService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Device identity helpers (Ed25519 key pair for gateway authentication)
# ---------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    """Base64url encode without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@dataclass(slots=True)
class DeviceIdentity:
    """Ed25519 device identity for OpenClaw gateway authentication."""

    device_id: str  # SHA-256 hex of raw public key
    public_key_pem: str
    private_key_pem: str


def _derive_device_id(raw_public_key_bytes: bytes) -> str:
    """Derive device ID as SHA-256 hex of the raw 32-byte Ed25519 public key."""
    return hashlib.sha256(raw_public_key_bytes).hexdigest()


def _extract_raw_public_key(pem: str) -> bytes:
    """Extract the raw 32-byte Ed25519 public key from a SPKI PEM block."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    pub = load_pem_public_key(pem.encode())
    return pub.public_bytes(Encoding.Raw, PublicFormat.Raw)


def _extract_raw_private_key(pem: str) -> bytes:
    """Extract the raw 32-byte Ed25519 private key from a PKCS8 PEM block."""
    from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat

    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    priv = load_pem_private_key(pem.encode(), password=None)
    return priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, None)


def load_or_create_identity(identity_path: Path) -> DeviceIdentity:
    """Load an existing device identity, or create and persist a new one."""
    if identity_path.exists():
        data = json.loads(identity_path.read_text(encoding="utf-8"))
        if data.get("version") == 1 and data.get("deviceId") and data.get("publicKeyPem") and data.get("privateKeyPem"):
            return DeviceIdentity(
                device_id=data["deviceId"],
                public_key_pem=data["publicKeyPem"],
                private_key_pem=data["privateKeyPem"],
            )

    # Generate a new Ed25519 key pair
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    raw_pub = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    device_id = _derive_device_id(raw_pub)

    public_key_pem = public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    private_key_pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()

    identity = DeviceIdentity(
        device_id=device_id,
        public_key_pem=public_key_pem,
        private_key_pem=private_key_pem,
    )

    # Persist
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    identity_path.write_text(
        json.dumps(
            {
                "version": 1,
                "deviceId": device_id,
                "publicKeyPem": public_key_pem,
                "privateKeyPem": private_key_pem,
                "createdAtMs": int(time.time() * 1000),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    identity_path.chmod(0o600)
    logger.info("Created new OpenClaw gateway device identity: %s", identity_path)
    return identity


def _resolve_identity_path(db: Database) -> Path:
    """Resolve the device identity file path from settings."""
    settings = db.get_settings()
    return settings.data_dir / "openclaw-device-identity.json"


def build_device_auth(
    identity: DeviceIdentity,
    *,
    client_id: str,
    client_mode: str,
    role: str,
    scopes: list[str],
    token: str,
    nonce: str,
    platform: str,
) -> dict[str, Any]:
    """Build the device auth fields for a gateway connect request.

    Constructs the v3 signature payload and signs it with the device's Ed25519 private key.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    signed_at_ms = int(time.time() * 1000)
    scopes_csv = ",".join(scopes)
    device_family = ""

    # v3 payload format
    payload = (
        f"v3|{identity.device_id}|{client_id}|{client_mode}|{role}"
        f"|{scopes_csv}|{signed_at_ms}|{token}|{nonce}|{platform.lower()}|{device_family}"
    )

    # Sign
    priv = load_pem_private_key(identity.private_key_pem.encode(), password=None)
    if not isinstance(priv, Ed25519PrivateKey):
        raise TypeError("Expected Ed25519 private key")
    signature = priv.sign(payload.encode("utf-8"))

    # Raw public key bytes, base64url encoded
    raw_pub = _extract_raw_public_key(identity.public_key_pem)

    return {
        "id": identity.device_id,
        "publicKey": _b64url(raw_pub),
        "signature": _b64url(signature),
        "signedAt": signed_at_ms,
        "nonce": nonce,
    }


class _GatewayConnection:
    """Persistent websocket connection to the OpenClaw gateway.

    Handles the connect handshake once, then multiplexes agent requests
    over the same connection using request ID correlation. Closes after
    an idle timeout to avoid holding connections indefinitely.
    """

    _IDLE_TIMEOUT = 300  # 5 minutes

    def __init__(self, service: OpenClawHookService) -> None:
        self._service = service
        self._ws: Any = None
        self._lock = asyncio.Lock()
        self._idle_timer: asyncio.Task | None = None

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        expect_final: bool = False,
        timeout_seconds: float | None = None,
        on_delta: Any | None = None,
        on_tool_start: Any | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            if self._idle_timer and not self._idle_timer.done():
                self._idle_timer.cancel()
                self._idle_timer = None
            await self._ensure_connected(timeout_seconds)
            request_id = str(uuid4())
            await self._ws.send(json.dumps({
                "type": "req",
                "id": request_id,
                "method": method,
                "params": params,
            }))
            result = await self._service._await_gateway_response(
                self._ws, request_id,
                timeout_seconds=timeout_seconds,
                expect_final=expect_final,
                on_delta=on_delta,
                on_tool_start=on_tool_start,
            )
            self._reset_idle_timer()
            return result

    async def _ensure_connected(self, timeout_seconds: float | None) -> None:
        from websockets.protocol import State
        if self._ws is not None and self._ws.state is State.OPEN:
            return
        await self._close()
        import websockets

        gateway_url = self._service.settings.resolved_gateway_url
        if not gateway_url:
            raise RuntimeError("OpenClaw gateway URL is not configured")

        timeout = timeout_seconds or self._service.settings.timeout_seconds
        self._ws = await websockets.connect(
            gateway_url, open_timeout=timeout, close_timeout=timeout, max_size=1_048_576,
        )
        nonce = await self._service._await_gateway_challenge(self._ws, timeout_seconds=timeout)
        connect_params = self._service._build_gateway_connect_params_with_device(nonce)
        connect_id = str(uuid4())
        await self._ws.send(json.dumps({
            "type": "req", "id": connect_id,
            "method": "connect", "params": connect_params,
        }))
        await self._service._await_gateway_response(self._ws, connect_id, timeout_seconds=timeout)
        logger.info("Persistent gateway connection established")

    def _reset_idle_timer(self) -> None:
        if self._idle_timer and not self._idle_timer.done():
            self._idle_timer.cancel()
        self._idle_timer = asyncio.create_task(self._idle_close())

    async def _idle_close(self) -> None:
        await asyncio.sleep(self._IDLE_TIMEOUT)
        logger.info("Gateway connection idle for %ds, closing", self._IDLE_TIMEOUT)
        await self._close()

    async def _close(self) -> None:
        if self._idle_timer and not self._idle_timer.done():
            self._idle_timer.cancel()
            self._idle_timer = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None


class OpenClawHookService(BaseService):
    """Send Cyborg notifications to OpenClaw via the gateway RPC surface."""

    MAX_RETRY_DELAY = timedelta(hours=6)
    GATEWAY_PROTOCOL_VERSION = 3
    GATEWAY_CLIENT_ID = "gateway-client"
    GATEWAY_CLIENT_MODE = "backend"
    GATEWAY_SCOPES = ["operator.write", "operator.admin"]
    BOOTSTRAP_TIMEOUT_SECONDS = 10800.0
    DISPATCH_ACCEPT_TIMEOUT = 30.0  # Max wait for gateway to accept a notification

    _WHATSAPP_DELIVERABLE_TYPES: frozenset[str] = frozenset({
        NotificationType.NEEDS_INPUT.value,
        NotificationType.PROJECT_RESULT.value,
    })

    _EMAIL_DELIVERABLE_TYPES: frozenset[str] = frozenset({
        NotificationType.NEEDS_INPUT.value,
        NotificationType.PROJECT_RESULT.value,
    })

    def __init__(
        self,
        ctx: AppContext,
        routing_service: SessionRouteService | None = None,
        cyborg_service_url: str | None = None,
    ) -> None:
        super().__init__(ctx)
        self._routing_service = routing_service
        self._cyborg_service_url = cyborg_service_url
        self._gateway_conn: _GatewayConnection | None = None

    @property
    def routing_service(self) -> SessionRouteService:
        if self._routing_service is None:
            self._routing_service = SessionRouteService(self.ctx)
        return self._routing_service

    @property
    def settings(self) -> OpenClawHookSettings:
        return self._get_settings().openclaw

    @property
    def cyborg_service_url(self) -> str | None:
        """Return the Cyborg service URL for callbacks."""
        return self._cyborg_service_url

    def is_configured(self) -> bool:
        return self.settings.enabled

    async def dispatch_notification(self, notification: dict[str, Any]) -> str | None:
        """Dispatch a user-facing notification (needs_input, project_result, etc.).

        Agent dispatch types (task_assignment, task_retry, etc.) now go through
        the dispatch system directly and should not reach this method.
        """
        is_retry = int(notification.get("delivery_attempt_count") or 1) > 1
        metadata = notification.get("metadata", {})

        route = await self.routing_service.resolve_notification_route(metadata)
        if route is None:
            self.logger.info(
                "Skipping %s notification — no delivery route (entity=%s)",
                notification.get("notification_type"), notification.get("entity_id"),
            )
            return None

        route_data = route.model_dump(mode="json")
        is_channel_less = route_data.get("channel") is None

        # User-facing notifications require a channel; skip silently for routeless projects
        if is_channel_less:
            self.logger.info(
                "Skipping %s notification — no channel route (entity=%s)",
                notification.get("notification_type"), notification.get("entity_id"),
            )
            return None

        # Email channel: deliver directly via AgentMail and dispatch to OpenClaw gateway
        if route_data.get("channel") == "email":
            await self._dispatch_email_notification(notification, route_data)
            return self._resolve_visible_session_key(route_data)

        # WhatsApp delivery filter — only specific types reach the user's channel
        if not self._is_whatsapp_deliverable(notification):
            self.logger.info(
                "Skipping WhatsApp delivery for %s notification (id=%s)",
                notification.get("notification_type"), notification.get("id"),
            )
            return None

        # Needs input notifications use agent method for context
        if notification.get("notification_type") == "needs_input":
            session_key = self._resolve_visible_session_key(route_data)
            if not is_retry:
                await log_prompt(
                    self.db,
                    category="needs_input",
                    prompt_text=self._render_needs_input_prompt(notification, route_data, session_key),
                    project_id=metadata.get("parent_project_id") or metadata.get("project_id"),
                    task_id=metadata.get("task_id") or notification.get("entity_id"),
                    session_key=session_key,
                )
            await self._send_gateway_request(
                "agent",
                self._build_needs_input_agent_params(notification, route_data, session_key),
                timeout_seconds=self.DISPATCH_ACCEPT_TIMEOUT,
            )
            return session_key

        # Generic send for remaining user-facing types (project_result, event_reminder)
        visible_session_key = self._resolve_visible_session_key(route_data)

        if not is_retry:
            await log_prompt(
                self.db,
                category="notification",
                prompt_text=self._render_message(notification),
                project_id=metadata.get("parent_project_id") or metadata.get("project_id"),
                task_id=metadata.get("task_id") or notification.get("entity_id"),
                session_key=visible_session_key,
            )
        await self._send_gateway_request(
            "send",
            self._build_send_params(
                notification,
                route_data,
                session_key=visible_session_key,
            ),
        )
        return None

    async def mark_delivery_success(self, notification_id: str, *, timestamp: str | None = None) -> None:
        now = timestamp or utcnow().isoformat()
        await self.db.execute(
            """
            UPDATE notifications
            SET delivery_status = ?, status = ?, acknowledged_at = ?, acknowledged_by = ?,
                last_delivery_at = ?, last_delivery_error = NULL, next_delivery_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (
                NotificationDeliveryStatus.DELIVERED.value,
                "acknowledged",
                now,
                "delivery",
                now,
                now,
                notification_id,
            ),
        )

    async def mark_delivery_failure(
        self,
        notification_id: str,
        attempt_count: int,
        error: str,
        *,
        notification_type: str | None = None,
        timestamp: str | None = None,
    ) -> None:
        now = utcnow()
        if timestamp is not None:
            now = type(now).fromisoformat(timestamp)

        # Agent-type dispatches (task_assignment, needs_input) send a full prompt
        # to OpenClaw. Retrying too quickly sends duplicate prompts that confuse
        # the agent. Wait at least 1 hour between retries for these.
        if notification_type in ("task_assignment", "needs_input", "task_retry", "task_tap", "next_action"):
            delay = timedelta(hours=max(1, min(6, attempt_count)))
        else:
            delay = timedelta(minutes=min(360, max(1, 2 ** max(attempt_count - 1, 0))))
        if delay > self.MAX_RETRY_DELAY:
            delay = self.MAX_RETRY_DELAY
        next_retry = (now + delay).isoformat()
        await self.db.execute(
            """
            UPDATE notifications
            SET delivery_status = ?, last_delivery_error = ?, next_delivery_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                NotificationDeliveryStatus.FAILED.value,
                error,
                next_retry,
                now.isoformat(),
                notification_id,
            ),
        )

    async def close(self) -> None:
        if self._gateway_conn is not None:
            await self._gateway_conn._close()
            self._gateway_conn = None

    async def _resolve_project_session_key(self, notification: dict[str, Any]) -> str | None:
        """Resolve a project's subagent_session_key from the notification's entity_id."""
        entity_type = notification.get("entity_type")
        if entity_type != "project":
            return None
        entity_id = str(notification.get("entity_id", ""))
        if not entity_id:
            return None
        return await self.resolve_project_session_key(entity_id)

    async def resolve_project_session_key(self, project_id: str) -> str | None:
        """Resolve a project's subagent_session_key."""
        project = await self.db.fetch_one(
            "SELECT subagent_session_key FROM projects WHERE id = ? AND deleted_at IS NULL",
            (project_id,),
        )
        return project.get("subagent_session_key") if project else None

    async def _dispatch_email_notification(
        self,
        notification: dict[str, Any],
        route: dict[str, Any],
    ) -> None:
        """Deliver a notification via email (AgentMail) and dispatch to OpenClaw gateway."""
        from cyborg_server.services.email_delivery_service import EmailDeliveryService

        metadata = notification.get("metadata", {})
        route_metadata = route.get("metadata", {})
        inbox_id = route_metadata.get("inbox_id")
        session_key = route.get("session_key")

        # Send email via AgentMail if this is a deliverable type
        if notification.get("notification_type") in self._EMAIL_DELIVERABLE_TYPES and inbox_id:
            delivery_service = EmailDeliveryService(self.ctx)
            message = self._render_message(notification)
            thread_id = route.get("chat_id")
            try:
                if thread_id:
                    await delivery_service.send_reply(
                        inbox_id=inbox_id,
                        thread_id=thread_id,
                        text=message,
                    )
                self.logger.info(
                    "Delivered %s notification via email (thread=%s)",
                    notification.get("notification_type"), thread_id,
                )
            except Exception:
                self.logger.warning(
                    "Failed to deliver email for notification %s",
                    notification.get("id"),
                    exc_info=True,
                )

    def _resolve_visible_session_key(self, route: dict[str, Any]) -> str | None:
        session_key = route.get("session_key")
        if isinstance(session_key, str) and session_key.strip():
            return session_key.strip()
        return None

    async def prepare_agent_dispatch(
        self,
        *,
        message: str,
        session_key: str,
        idempotency_key: str,
        timeout_seconds: float | None = None,
        deliver: bool = False,
    ) -> Coroutine:
        """Return a gateway coroutine for dispatch_service.track() to await.

        Callers build the prompt, resolve the session key, then pass the
        coroutine to DispatchService.track() for lifecycle management.
        """
        timeout = timeout_seconds or int(max(self.BOOTSTRAP_TIMEOUT_SECONDS, self.settings.timeout_seconds))
        params: dict[str, Any] = {
            "message": message,
            "deliver": deliver,
            "sessionKey": session_key,
            "thinking": "on",
            "timeout": timeout,
            "idempotencyKey": idempotency_key,
        }
        if self.settings.agent_id:
            params["agentId"] = self.settings.agent_id
        return self._send_gateway_request(
            "agent", params, expect_final=True, timeout_seconds=timeout,
        )

    async def patch_session_model(self, session_key: str) -> None:
        if not self.settings.voice_model:
            return
        await self._send_gateway_request_persistent(
            "sessions.patch",
            {"key": session_key, "model": self.settings.voice_model},
        )

    async def prepare_streaming_agent_dispatch(
        self,
        *,
        message: str,
        session_key: str,
        idempotency_key: str,
        on_delta: Any | None = None,
        on_tool_start: Any | None = None,
        timeout_seconds: float | None = None,
    ) -> Coroutine:
        """Return a gateway coroutine with streaming callbacks for dispatch_service.track().

        Uses a persistent websocket connection to avoid re-handshaking on every
        voice dispatch, eliminating queued-message issues and reducing latency.
        """
        timeout = timeout_seconds or int(max(self.BOOTSTRAP_TIMEOUT_SECONDS, self.settings.timeout_seconds))
        params: dict[str, Any] = {
            "message": message,
            "deliver": False,
            "sessionKey": session_key,
            "thinking": "off",
            "timeout": timeout,
            "idempotencyKey": idempotency_key,
        }
        if self.settings.agent_id:
            params["agentId"] = self.settings.agent_id
        return self._send_gateway_request_persistent(
            "agent", params, expect_final=True, timeout_seconds=timeout,
            on_delta=on_delta, on_tool_start=on_tool_start,
        )

    async def _send_gateway_request_persistent(
        self,
        method: str,
        params: dict[str, Any],
        *,
        expect_final: bool = False,
        timeout_seconds: float | None = None,
        on_delta: Any | None = None,
        on_tool_start: Any | None = None,
    ) -> dict[str, Any]:
        """Send a request over the persistent gateway connection.

        Creates the connection on first use, reuses it for subsequent requests,
        and reconnects with one retry if the connection drops mid-request.
        """
        import websockets

        if self._gateway_conn is None:
            self._gateway_conn = _GatewayConnection(self)
        try:
            return await self._gateway_conn.request(
                method, params,
                expect_final=expect_final,
                timeout_seconds=timeout_seconds,
                on_delta=on_delta,
                on_tool_start=on_tool_start,
            )
        except (websockets.ConnectionClosed, websockets.InvalidState):
            logger.warning("Persistent gateway connection dropped, reconnecting")
            await self._gateway_conn._close()
            return await self._gateway_conn.request(
                method, params,
                expect_final=expect_final,
                timeout_seconds=timeout_seconds,
                on_delta=on_delta,
                on_tool_start=on_tool_start,
            )

    async def _send_gateway_request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        expect_final: bool = False,
        timeout_seconds: float | None = None,
        on_delta: Any | None = None,
        on_tool_start: Any | None = None,
    ) -> dict[str, Any]:
        return await self._send_gateway_request_via_websocket(
            method,
            params,
            expect_final=expect_final,
            timeout_seconds=timeout_seconds,
            on_delta=on_delta,
            on_tool_start=on_tool_start,
        )

    async def _send_gateway_request_via_websocket(
        self,
        method: str,
        params: dict[str, Any],
        *,
        expect_final: bool,
        timeout_seconds: float | None,
        on_delta: Any | None = None,
        on_tool_start: Any | None = None,
    ) -> dict[str, Any]:
        gateway_url = self.settings.resolved_gateway_url
        if not gateway_url:
            raise RuntimeError("OpenClaw gateway URL is not configured")

        import websockets

        timeout = timeout_seconds or self.settings.timeout_seconds
        connect_id = str(uuid4())
        request_id = str(uuid4())
        async with websockets.connect(
            gateway_url,
            open_timeout=timeout,
            close_timeout=timeout,
            max_size=1_048_576,
        ) as websocket:
            nonce = await self._await_gateway_challenge(websocket, timeout_seconds=timeout)

            connect_params = self._build_gateway_connect_params_with_device(nonce)
            await websocket.send(
                json.dumps(
                    {
                        "type": "req",
                        "id": connect_id,
                        "method": "connect",
                        "params": connect_params,
                    }
                )
            )
            await self._await_gateway_response(websocket, connect_id, timeout_seconds=timeout)

            await websocket.send(
                json.dumps(
                    {
                        "type": "req",
                        "id": request_id,
                        "method": method,
                        "params": params,
                    }
                )
            )
            return await self._await_gateway_response(
                websocket,
                request_id,
                timeout_seconds=timeout,
                expect_final=expect_final,
                on_delta=on_delta,
                on_tool_start=on_tool_start,
            )

    def _build_gateway_connect_params(self) -> dict[str, Any]:
        connect_params: dict[str, Any] = {
            "minProtocol": self.GATEWAY_PROTOCOL_VERSION,
            "maxProtocol": self.GATEWAY_PROTOCOL_VERSION,
            "client": {
                "id": self.GATEWAY_CLIENT_ID,
                "displayName": "Cyborg",
                "version": __version__,
                "platform": "python",
                "mode": self.GATEWAY_CLIENT_MODE,
                "instanceId": str(uuid4()),
            },
            "role": "operator",
            "scopes": self.GATEWAY_SCOPES,
            "caps": [],
            "commands": [],
            "permissions": {},
            "userAgent": f"cyborg/{__version__}",
        }
        gateway_token = self.settings.resolved_gateway_token
        if gateway_token:
            connect_params["auth"] = {"token": gateway_token}
        return connect_params

    def _build_gateway_connect_params_with_device(self, nonce: str) -> dict[str, Any]:
        """Build connect params including Ed25519 device identity and signature."""
        connect_params = self._build_gateway_connect_params()
        gateway_token = self.settings.resolved_gateway_token

        try:
            identity_path = _resolve_identity_path(self.db)
            identity = load_or_create_identity(identity_path)
            device_auth = build_device_auth(
                identity,
                client_id=self.GATEWAY_CLIENT_ID,
                client_mode=self.GATEWAY_CLIENT_MODE,
                role="operator",
                scopes=self.GATEWAY_SCOPES,
                token=gateway_token,
                nonce=nonce,
                platform="python",
            )
            connect_params["device"] = device_auth
        except Exception:
            logger.warning("Failed to build device auth, connecting without device identity", exc_info=True)

        return connect_params

    async def _await_gateway_challenge(self, websocket: Any, *, timeout_seconds: float) -> str:
        """Wait for the gateway connect challenge and return the nonce."""
        timeout = timeout_seconds
        while True:
            raw = await asyncio.wait_for(websocket.recv(), timeout=timeout)
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"OpenClaw gateway returned invalid JSON: {raw!r}") from exc

            if frame.get("type") != "event":
                if frame.get("type") == "res" and frame.get("ok") is False:
                    error = frame.get("error")
                    if isinstance(error, dict):
                        message = error.get("message") or json.dumps(error)
                    else:
                        message = str(error)
                    raise RuntimeError(f"OpenClaw gateway connect challenge failed: {message}")
                continue

            if frame.get("event") != "connect.challenge":
                continue

            payload = frame.get("payload")
            nonce = payload.get("nonce") if isinstance(payload, dict) else None
            if not isinstance(nonce, str) or not nonce.strip():
                raise RuntimeError("OpenClaw gateway connect challenge missing nonce")
            return nonce

    async def _await_gateway_response(
        self,
        websocket: Any,
        expected_id: str,
        *,
        timeout_seconds: float,
        expect_final: bool = False,
        on_delta: Any | None = None,
        on_tool_start: Any | None = None,
    ) -> dict[str, Any]:
        timeout = timeout_seconds
        while True:
            raw = await asyncio.wait_for(websocket.recv(), timeout=timeout)
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"OpenClaw gateway returned invalid JSON: {raw!r}") from exc

            if frame.get("type") == "event":
                if on_delta is not None or on_tool_start is not None:
                    if frame.get("event") == "agent":
                        payload = frame.get("payload")
                        if isinstance(payload, dict):
                            stream = payload.get("stream")
                            data = payload.get("data")
                            if on_delta is not None and stream == "assistant" and isinstance(data, dict):
                                text = data.get("text", "")
                                if text:
                                    await on_delta(text)
                            if on_tool_start is not None and stream == "item" and isinstance(data, dict):
                                if data.get("kind") == "tool" and data.get("phase") == "start":
                                    await on_tool_start()
                continue
            if frame.get("type") != "res" or frame.get("id") != expected_id:
                continue
            payload = frame.get("payload")
            if expect_final and isinstance(payload, dict) and payload.get("status") == "accepted":
                continue
            if frame.get("ok") is True:
                return payload if isinstance(payload, dict) else {"payload": payload}

            error = frame.get("error")
            if isinstance(error, dict):
                message = error.get("message") or json.dumps(error)
            else:
                message = str(error)
            raise RuntimeError(f"OpenClaw gateway {expected_id} failed: {message}")

    def _build_send_params(
        self,
        notification: dict[str, Any],
        route: dict[str, Any],
        *,
        session_key: str | None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "channel": route["channel"],
            "to": route["to"],
            "message": self._render_message(notification),
            "idempotencyKey": notification["id"],
        }
        if self.settings.agent_id:
            params["agentId"] = self.settings.agent_id
        if session_key:
            params["sessionKey"] = session_key
        return params

    def _build_needs_input_agent_params(
        self,
        notification: dict[str, Any],
        route: dict[str, Any],
        session_key: str,
    ) -> dict[str, Any]:
        """Build agent params for needs_input notifications (plan approvals, etc.)"""
        timeout_seconds = int(max(self.BOOTSTRAP_TIMEOUT_SECONDS, self.settings.timeout_seconds))
        params: dict[str, Any] = {
            "message": self._render_needs_input_prompt(notification, route, session_key),
            "deliver": route.get("channel") is not None,
            "sessionKey": session_key,
            "thinking": "on",
            "timeout": timeout_seconds,
            "idempotencyKey": notification["id"],
        }
        if route.get("channel"):
            params["channel"] = route["channel"]
        if route.get("to"):
            params["to"] = route["to"]
        if self.settings.agent_id:
            params["agentId"] = self.settings.agent_id
        return params

    def _render_message(self, notification: dict[str, Any]) -> str:
        parts = [notification["title"], "", notification["message"]]
        if notification.get("entity_type") == "task" and notification.get("metadata", {}).get("parent_project_title"):
            parts.extend(
                [
                    "",
                    f"Project: {notification['metadata']['parent_project_title']}",
                ]
            )
        parts.extend(
            [
                "",
                f"Notification ID: {notification['id']}",
            ]
        )
        if self.cyborg_service_url:
            parts.extend(
                [
                    "",
                    f"Cyborg Service: {self.cyborg_service_url}",
                ]
            )
        return "\n".join(part for part in parts if part is not None)

    def _render_needs_input_prompt(
        self,
        notification: dict[str, Any],
        route: dict[str, Any],
        session_key: str,
    ) -> str:
        """Render prompt for needs_input notifications (plan approvals, etc.)"""
        metadata = notification.get("metadata", {})
        lines = [
            "Cyborg notification: approval or input needed.",
            "",
            "The user needs to review and respond to a Cyborg request.",
            "Your task is to:",
            "1. Show thinking about what needs approval",
            "2. Present the request clearly to the user",
            "3. Help them understand what action is needed",
            "",
            f"Notification ID: {notification['id']}",
            f"Type: {notification.get('notification_type', 'unknown')}",
            "",
            f"Request: {notification['title']}",
            "",
            notification["message"],
        ]
        if metadata.get("task_id"):
            lines.extend([
                "",
                f"Task ID: {metadata['task_id']}",
            ])
        if metadata.get("parent_project_title"):
            lines.extend([
                "",
                f"Project: {metadata['parent_project_title']} ({metadata.get('parent_project_id')})",
            ])
        if metadata.get("blocked_reason"):
            lines.extend([
                "",
                f"Blocked reason: {metadata['blocked_reason']}",
            ])
        if metadata.get("blocked_resume_instructions"):
            lines.extend([
                "",
                f"Resume instructions: {metadata['blocked_resume_instructions']}",
            ])
        lines.extend([
            "",
            "Instructions:",
            "- Send a natural message to the recipient asking for the needed approval/input.",
            "- Include relevant details from the request above.",
            "- Do not mention Cyborg internal details like notification IDs unless necessary.",
            "- Keep the tone appropriate for the channel (WhatsApp DM).",
        ])
        # Include instructions for how to respond
        if metadata.get("task_id"):
            task_id = metadata['task_id']
            lines.extend([
                "",
                f"Once the user approves, respond to this notification by calling: cyborg task plan approve {task_id}",
                f"Or use the HTTP API: PUT /api/v1/tasks/{task_id}/plan with plan approval details.",
            ])
        return "\n".join(lines)

    def _is_whatsapp_deliverable(self, notification: dict[str, Any]) -> bool:
        """Check if this notification type should be delivered to the user's WhatsApp channel."""
        return notification.get("notification_type") in self._WHATSAPP_DELIVERABLE_TYPES
