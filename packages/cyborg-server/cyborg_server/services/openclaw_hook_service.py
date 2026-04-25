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
from typing import Any
from uuid import uuid4

from cyborg_server import __version__
from cyborg_server.config import OpenClawHookSettings, Settings
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
    settings = getattr(db, "settings", None)
    if isinstance(settings, Settings):
        return settings.data_dir / "openclaw-device-identity.json"
    return Path("~/.local/share/cyborg/openclaw-device-identity.json").expanduser()


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


class OpenClawHookService(BaseService):
    """Send Cyborg notifications to OpenClaw via the gateway RPC surface."""

    MAX_RETRY_DELAY = timedelta(hours=6)
    GATEWAY_PROTOCOL_VERSION = 3
    GATEWAY_CLIENT_ID = "gateway-client"
    GATEWAY_CLIENT_MODE = "backend"
    GATEWAY_SCOPES = ["operator.write"]
    BOOTSTRAP_TIMEOUT_SECONDS = 10800.0
    DISPATCH_ACCEPT_TIMEOUT = 30.0  # Max wait for gateway to accept a notification

    def __init__(
        self,
        db: Database,
        routing_service: SessionRouteService | None = None,
        cyborg_service_url: str | None = None,
    ) -> None:
        super().__init__(db)
        self._routing_service = routing_service
        self._cyborg_service_url = cyborg_service_url

    @property
    def routing_service(self) -> SessionRouteService:
        if self._routing_service is None:
            self._routing_service = SessionRouteService(self.db)
        return self._routing_service

    @property
    def settings(self) -> OpenClawHookSettings:
        current = getattr(self.db, "settings", None)
        if isinstance(current, Settings):
            return current.openclaw
        return Settings.from_env().openclaw

    @property
    def cyborg_service_url(self) -> str | None:
        """Return the Cyborg service URL for callbacks."""
        return self._cyborg_service_url

    def is_configured(self) -> bool:
        return self.settings.enabled

    async def dispatch_notification(self, notification: dict[str, Any]) -> None:
        is_retry = int(notification.get("delivery_attempt_count") or 1) > 1
        metadata = notification.get("metadata", {})
        route = await self.routing_service.resolve_notification_route(metadata)
        if route is None:
            if metadata.get("auto_created_by_project"):
                self.logger.info("Skipping notification for auto-created task — no delivery route available")
                return
            raise ValueError("No delivery route could be resolved for the notification")

        route_data = route.model_dump(mode="json")
        is_channel_less = route_data.get("channel") is None

        # User-facing notifications require a channel; agent dispatches do not
        if is_channel_less and not self._is_agent_dispatch_type(notification):
            raise ValueError("No channel route could be resolved for user-facing notification")

        delivery_session_key = await self._resolve_delivery_session_key(notification, route_data)

        # Task assignments and plan approvals use the agent method with detailed prompt
        if self._should_use_task_assignment_agent(notification, delivery_session_key):
            if not is_retry:
                await log_prompt(
                    self.db,
                    category="task_assignment",
                    prompt_text=await self._render_task_assignment_prompt(notification, route_data, delivery_session_key),
                    project_id=metadata.get("parent_project_id") or metadata.get("project_id"),
                    task_id=metadata.get("task_id") or notification.get("entity_id"),
                    session_key=delivery_session_key,
                )
            await self._send_gateway_request(
                "agent",
                await self._build_task_assignment_agent_params(notification, route_data, delivery_session_key),
                timeout_seconds=self.DISPATCH_ACCEPT_TIMEOUT,
            )
            return

        # Needs input notifications (plan approvals, etc.) also use agent method for context
        if notification.get("notification_type") == "needs_input":
            session_key = delivery_session_key or self._resolve_visible_session_key(route_data)
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
            return

        # Task retry notifications use the agent method with retry-specific prompt
        if notification.get("notification_type") == NotificationType.TASK_RETRY.value:
            session_key = delivery_session_key or self._resolve_visible_session_key(route_data)
            if not is_retry:
                await log_prompt(
                    self.db,
                    category="task_retry",
                    prompt_text=self._render_task_retry_prompt(notification, route_data, session_key),
                    project_id=metadata.get("parent_project_id") or metadata.get("project_id"),
                    task_id=metadata.get("task_id") or notification.get("entity_id"),
                    session_key=session_key,
                )
            await self._send_gateway_request(
                "agent",
                self._build_task_retry_agent_params(notification, route_data, session_key),
                timeout_seconds=self.DISPATCH_ACCEPT_TIMEOUT,
            )
            return

        # Task input response notifications use the agent method with input-specific prompt
        if notification.get("notification_type") == NotificationType.TASK_INPUT_RESPONSE.value:
            session_key = delivery_session_key or self._resolve_visible_session_key(route_data)
            if not is_retry:
                await log_prompt(
                    self.db,
                    category="task_input_response",
                    prompt_text=self._render_task_input_response_prompt(notification, route_data, session_key),
                    project_id=metadata.get("parent_project_id") or metadata.get("project_id"),
                    task_id=metadata.get("task_id") or notification.get("entity_id"),
                    session_key=session_key,
                )
            await self._send_gateway_request(
                "agent",
                self._build_task_input_response_agent_params(notification, route_data, session_key),
                timeout_seconds=self.DISPATCH_ACCEPT_TIMEOUT,
            )
            return

        # Task tap notifications nudge the agent to continue or submit
        if notification.get("notification_type") == NotificationType.TASK_TAP.value:
            session_key = delivery_session_key or self._resolve_visible_session_key(route_data)
            if not is_retry:
                await log_prompt(
                    self.db,
                    category="task_tap",
                    prompt_text=self._render_task_tap_prompt(notification, route_data, session_key),
                    project_id=metadata.get("parent_project_id") or metadata.get("project_id"),
                    task_id=metadata.get("task_id") or notification.get("entity_id"),
                    session_key=session_key,
                )
            await self._send_gateway_request(
                "agent",
                self._build_task_tap_agent_params(notification, route_data, session_key),
                timeout_seconds=self.DISPATCH_ACCEPT_TIMEOUT,
            )
            return

        # Submission review notifications use a fresh session for unbiased review
        if notification.get("notification_type") == NotificationType.SUBMISSION_REVIEW.value:
            # Derive a fresh review session key (not the task's execution session)
            task_id = metadata.get("task_id") or notification.get("entity_id", "")
            from cyborg_server.services.project_service import short_task_id
            review_session_key = f"cyborg:review:{short_task_id(task_id)}"
            if not is_retry:
                await log_prompt(
                    self.db,
                    category="submission_review",
                    prompt_text=self._render_submission_review_prompt(notification, route_data, review_session_key),
                    project_id=metadata.get("parent_project_id") or metadata.get("project_id"),
                    task_id=metadata.get("task_id") or notification.get("entity_id"),
                    session_key=review_session_key,
                )
            await self._send_gateway_request(
                "agent",
                self._build_submission_review_agent_params(notification, route_data, review_session_key),
                timeout_seconds=self.DISPATCH_ACCEPT_TIMEOUT,
            )
            return

        # Next-action prompts use a fresh reasoning session (not the source channel)
        if notification.get("notification_type") == NotificationType.NEXT_ACTION.value:
            from cyborg_server.services.project_service import short_task_id
            project_id = metadata.get("project_id", "")
            next_action_session_key = f"cyborg:next-action:{short_task_id(project_id)}"
            if not is_retry:
                await log_prompt(
                    self.db,
                    category="next_action",
                    prompt_text=notification["message"],
                    project_id=metadata.get("project_id"),
                    task_id=metadata.get("completed_task_id"),
                    session_key=next_action_session_key,
                )
            await self._send_gateway_request(
                "agent",
                self._build_next_action_agent_params(notification, route_data, next_action_session_key),
                timeout_seconds=self.DISPATCH_ACCEPT_TIMEOUT,
            )
            return

        visible_session_key = delivery_session_key or self._resolve_visible_session_key(route_data)

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
        return None

    async def _resolve_delivery_session_key(
        self,
        notification: dict[str, Any],
        route: dict[str, Any],
    ) -> str | None:
        if self._is_target_task_assignment(notification):
            session_key = await self.routing_service.resolve_target_session_key(notification.get("metadata", {}))
            if session_key is None:
                session_key = route.get("session_key")
            if session_key is None:
                raise ValueError("Task assignment delivery requires a resolvable target OpenClaw session key")
            return session_key
        if self._is_auto_project_source_task_assignment(notification):
            session_key = self._resolve_visible_session_key(route)
            if session_key is None:
                raise ValueError("Auto-created project task assignment requires a visible source OpenClaw session key")
            return session_key

        return None

    def _resolve_visible_session_key(self, route: dict[str, Any]) -> str | None:
        session_key = route.get("session_key")
        if isinstance(session_key, str) and session_key.strip():
            return session_key.strip()
        return None

    async def _send_gateway_request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        expect_final: bool = False,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return await self._send_gateway_request_via_websocket(
            method,
            params,
            expect_final=expect_final,
            timeout_seconds=timeout_seconds,
        )

    async def _send_gateway_request_via_websocket(
        self,
        method: str,
        params: dict[str, Any],
        *,
        expect_final: bool,
        timeout_seconds: float | None,
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
    ) -> dict[str, Any]:
        timeout = timeout_seconds
        while True:
            raw = await asyncio.wait_for(websocket.recv(), timeout=timeout)
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"OpenClaw gateway returned invalid JSON: {raw!r}") from exc

            if frame.get("type") == "event":
                # Pre-connect challenges and background events are not relevant here.
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

    async def _build_task_assignment_agent_params(
        self,
        notification: dict[str, Any],
        route: dict[str, Any],
        session_key: str,
    ) -> dict[str, Any]:
        timeout_seconds = int(max(self.BOOTSTRAP_TIMEOUT_SECONDS, self.settings.timeout_seconds))
        is_channel_less = route.get("channel") is None
        params: dict[str, Any] = {
            "message": await self._render_task_assignment_prompt(notification, route, session_key),
            "deliver": not is_channel_less,
            "sessionKey": session_key,
            "thinking": "high",
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
            "deliver": True,
            "channel": route["channel"],
            "to": route["to"],
            "sessionKey": session_key,
            "thinking": "high",
            "timeout": timeout_seconds,
            "idempotencyKey": notification["id"],
        }
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

    async def _render_task_assignment_prompt(
        self,
        notification: dict[str, Any],
        route: dict[str, Any],
        session_key: str,
    ) -> str:
        metadata = notification.get("metadata", {})
        target_session = metadata.get("target_session")
        is_internal = bool(metadata.get("auto_created_by_project"))
        task_id = metadata.get("task_id") or notification.get("entity_id")

        if is_internal:
            lines = [
                "Cyborg internal task assignment for this session.",
                "",
                "This is an auto-created project task. Work autonomously to complete it.",
                "Do NOT send messages to the user or wait for replies in this session.",
                "If you need human input, block the task using the block API with an input_schema.",
                "",
            ]
        else:
            lines = [
                "Cyborg task assignment for this session.",
                "",
                "You are responsible for handling this task in the current session.",
                "Use the user's replies here as task input, ask focused follow-up questions if needed,",
                "and complete or fail the Cyborg task once you have a clear answer.",
                "This turn should send the first natural user-facing message to the recipient.",
                "",
            ]
        lines.extend(
            [
                f"Task ID: {task_id}",
                f"Notification ID: {notification['id']}",
                f"Session Key: {session_key}",
                f"Task: {notification['title']}",
            ]
        )

        # Add description if present in notification message
        if notification.get("message"):
            lines.extend(["", notification["message"]])

        if metadata.get("parent_project_title"):
            lines.extend(
                [
                    "",
                    f"Parent project: {metadata['parent_project_title']} ({metadata.get('parent_project_id')})",
                ]
            )
        if metadata.get("requested_by"):
            lines.extend(["", f"Requested by: {metadata['requested_by']}"])

        # For internal project tasks, include prior completed tasks and their files
        if is_internal and metadata.get("parent_project_id"):
            prior_lines = await self._render_prior_work_section(metadata["parent_project_id"], task_id)
            if prior_lines:
                lines.extend([""] + prior_lines)

        if is_internal:
            lines.extend(
                [
                    "",
                    "Instructions:",
                    "- Work autonomously to complete the task. Do not send messages or wait for user replies.",
                    "- If you can complete the task independently, do so and submit the result.",
                    "- If you genuinely need human input, block the task with an input_schema:",
                    f"  POST {self.cyborg_service_url or ''}/api/v1/tasks/{task_id}/block"
                    '  {"reason":"<why blocked>","resume_instructions":"<how to resume>",'
                    '"input_schema":{"type":"text","prompt":"<question>"}}',
                    "  (input_schema can also be"
                    ' {"type":"multi_choice","prompt":"...","options":[{"label":"A","value":"a"}],"allow_multiple":false})',
                    "- The block creates an approval in the Cyborg dashboard where the user can respond.",
                    "- When the user responds, you will receive a task_input_response notification to resume.",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    "Instructions:",
                    "- Send one concise natural message now that asks the first question needed to progress the task.",
                    "- Do not mention Cyborg, hidden setup, task IDs, notification IDs, or internal routing.",
                    "- Treat the next user reply in this session as work on this task.",
                    "- If the answer is incomplete, ask one focused follow-up at a time.",
                ]
            )

        # Include output directory instructions if available
        output_directory = metadata.get("output_directory")
        if output_directory:
            lines.extend(
                [
                    "",
                    "## Output Directory",
                    f"All task artifacts must be written to: `{output_directory}`",
                    "- Use descriptive filenames for each artifact.",
                    "- Put the primary result in `RESULT.md`.",
                ]
            )
            if self.cyborg_service_url:
                lines.extend(
                    [
                        f"- Register all output files via the API: POST {self.cyborg_service_url}/api/v1/tasks/{task_id}/files",
                    ]
                )
            else:
                lines.extend(
                    [
                        "- Register all output files via the API: POST /api/v1/tasks/{task_id}/files",
                    ]
                )
        # Include API completion instructions with service URL
        if self.cyborg_service_url:
            lines.extend(
                [
                    f'- Once the task is done, submit the task for review by calling: cyborg task submit <task-id> --result-summary "<answer>"',
                    f'- Or use the HTTP API: POST {self.cyborg_service_url}/api/v1/tasks/<task-id>/submit with JSON {{"result_summary":"<answer>"}}',
                ]
            )
        else:
            lines.extend(
                [
                    '- Once the task is done, submit the task for review using: cyborg task submit <task-id> --result-summary "<answer>".',
                    '- If you use the HTTP API instead of the CLI, POST to /api/v1/tasks/<task-id>/submit with JSON {"result_summary":"<answer>"}.',
                ]
            )
        if not is_internal:
            lines.extend(
                [
                    "- Keep the tone natural for the channel and recipient.",
                ]
            )
        return "\n".join(lines)

    async def _render_prior_work_section(
        self,
        project_id: str,
        exclude_task_id: str,
    ) -> list[str]:
        """Render a section listing prior completed tasks and their files in a project."""
        completed_tasks = await self.db.fetch_all(
            """
            SELECT t.id, t.title, t.result
            FROM tasks t
            INNER JOIN project_tasks pt ON pt.task_id = t.id
            WHERE pt.project_id = ?
              AND t.status = 'completed'
              AND t.deleted_at IS NULL
              AND t.id != ?
            ORDER BY t.completed_at ASC
            """,
            (project_id, exclude_task_id),
        )
        if not completed_tasks:
            return []

        lines = ["## Prior work in this project"]
        for task in completed_tasks:
            result_bit = f": {task['result'][:200]}" if task.get("result") else ""
            lines.append(f"  - {task['title']}{result_bit}")

            # Fetch files for this task
            files = await self.db.fetch_all(
                "SELECT filename, relative_path, purpose, description FROM task_files WHERE task_id = ? ORDER BY created_at ASC",
                (task["id"],),
            )
            if files:
                for f in files:
                    desc = f" ({f['description']})" if f.get("description") else ""
                    lines.append(f"    - {f['relative_path']} [{f['purpose']}]{desc}")

        return lines

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

    def _render_task_retry_prompt(
        self,
        notification: dict[str, Any],
        route: dict[str, Any],
        session_key: str,
    ) -> str:
        """Render prompt for task_retry notifications (submission rejected)."""
        metadata = notification.get("metadata", {})
        review_feedback = metadata.get("review_feedback", {})
        issues = review_feedback.get("issues", [])
        suggestions = review_feedback.get("suggestions", [])
        reasoning = review_feedback.get("reasoning", "")

        lines = [
            "Cyborg task retry: the previous submission for this task was rejected by review.",
            "",
            "You must address the issues below and re-submit the task.",
            "",
            f"Task ID: {metadata.get('task_id') or notification.get('entity_id')}",
            f"Notification ID: {notification['id']}",
            f"Session Key: {session_key}",
            f"Task: {notification['title']}",
            "",
            "## Review Feedback",
            f"Reason: {reasoning}",
        ]

        if issues:
            lines.extend(["", "Issues found:"])
            for issue in issues:
                lines.append(f"  - {issue}")

        if suggestions:
            lines.extend(["", "Specific suggestions:"])
            for suggestion in suggestions:
                lines.append(f"  - {suggestion}")

        lines.extend([
            "",
            "## Instructions",
            "- Address each issue raised by the review.",
            "- Create any missing files that were expected.",
            "- Do the actual work required by the task, don't just claim it's done.",
        ])

        # Include output directory instructions if available
        output_directory = metadata.get("output_directory")
        if output_directory:
            task_id = metadata.get("task_id") or notification.get("entity_id")
            lines.extend([
                "",
                "## Output Directory",
                f"All task artifacts must be written to: `{output_directory}`",
                "- Use descriptive filenames for each artifact.",
                "- Put the primary result in `RESULT.md`.",
            ])
            if self.cyborg_service_url:
                lines.append(
                    f"- Register all output files via the API: POST {self.cyborg_service_url}/api/v1/tasks/{task_id}/files"
                )

        # Submit instructions
        if self.cyborg_service_url:
            lines.extend([
                "",
                f'- When finished, submit the task by calling: cyborg task submit {metadata.get("task_id", "<task-id>")} --result-summary "<answer>"',
                f'- Or use the HTTP API: POST {self.cyborg_service_url}/api/v1/tasks/{metadata.get("task_id", "<task-id>")}/submit with JSON {{"result_summary":"<answer>"}}',
            ])
        else:
            lines.extend([
                "",
                f'- When finished, submit the task using: cyborg task submit {metadata.get("task_id", "<task-id>")} --result-summary "<answer>".',
                '- If you use the HTTP API instead of the CLI, POST to /api/v1/tasks/<task-id>/submit with JSON {"result_summary":"<answer>"}.',
            ])

        return "\n".join(lines)

    def _build_task_retry_agent_params(
        self,
        notification: dict[str, Any],
        route: dict[str, Any],
        session_key: str,
    ) -> dict[str, Any]:
        timeout_seconds = int(max(self.BOOTSTRAP_TIMEOUT_SECONDS, self.settings.timeout_seconds))
        params: dict[str, Any] = {
            "message": self._render_task_retry_prompt(notification, route, session_key),
            "deliver": route.get("channel") is not None,
            "sessionKey": session_key,
            "thinking": "high",
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

    def _render_task_input_response_prompt(
        self,
        notification: dict[str, Any],
        route: dict[str, Any],
        session_key: str,
    ) -> str:
        """Render prompt for task_input_response notifications (user answered an input request)."""
        metadata = notification.get("metadata", {})
        input_response = metadata.get("input_response", "")
        input_prompt = metadata.get("input_prompt", "")

        if isinstance(input_response, list):
            response_text = ", ".join(input_response)
        else:
            response_text = str(input_response)

        lines = [
            "Cyborg task input: the user has responded to your question.",
            "",
            "You asked the user a question and they have provided their answer.",
            "Resume working on this task using their response.",
            "",
            f"Task ID: {metadata.get('task_id') or notification.get('entity_id')}",
            f"Notification ID: {notification['id']}",
            f"Session Key: {session_key}",
            f"Task: {notification['title']}",
            "",
            "## Your Question",
            input_prompt or "(question not available)",
            "",
            "## User's Response",
            response_text,
        ]

        if metadata.get("parent_project_title"):
            lines.extend([
                "",
                f"Parent project: {metadata['parent_project_title']} ({metadata.get('parent_project_id')})",
            ])

        # Include output directory instructions if available
        output_directory = metadata.get("output_directory")
        if output_directory:
            task_id = metadata.get("task_id") or notification.get("entity_id")
            lines.extend([
                "",
                "## Output Directory",
                f"All task artifacts must be written to: `{output_directory}`",
                "- Use descriptive filenames for each artifact.",
                "- Put the primary result in `RESULT.md`.",
            ])
            if self.cyborg_service_url:
                lines.append(
                    f"- Register all output files via the API: POST {self.cyborg_service_url}/api/v1/tasks/{task_id}/files"
                )

        # Submit instructions
        if self.cyborg_service_url:
            lines.extend([
                "",
                "## Instructions",
                "- Use the user's response to continue working on the task.",
                f"- When done, submit: POST {self.cyborg_service_url}/api/v1/tasks/{metadata.get('task_id', '<task-id>')}/submit with JSON {{\"result_summary\":\"<answer>\"}}",
            ])
        else:
            lines.extend([
                "",
                "## Instructions",
                "- Use the user's response to continue working on the task.",
                f"- When done, submit using: cyborg task submit {metadata.get('task_id', '<task-id>')} --result-summary \"<answer>\".",
            ])

        return "\n".join(lines)

    def _build_task_input_response_agent_params(
        self,
        notification: dict[str, Any],
        route: dict[str, Any],
        session_key: str,
    ) -> dict[str, Any]:
        timeout_seconds = int(max(self.BOOTSTRAP_TIMEOUT_SECONDS, self.settings.timeout_seconds))
        params: dict[str, Any] = {
            "message": self._render_task_input_response_prompt(notification, route, session_key),
            "deliver": route.get("channel") is not None,
            "sessionKey": session_key,
            "thinking": "high",
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

    def _render_task_tap_prompt(
        self,
        notification: dict[str, Any],
        route: dict[str, Any],
        session_key: str,
    ) -> str:
        """Render prompt for task_tap notifications (operator nudge)."""
        metadata = notification.get("metadata", {})
        task_id = metadata.get("task_id") or notification.get("entity_id")

        lines = [
            "Cyborg task status check: the operator is nudging you on an active task.",
            "",
            "Please check your progress. If you have finished, register any output files and submit the task.",
            "If you are still working, continue — no extra action needed beyond eventual completion.",
            "If you are stuck or need clarification, block the task with an input_schema.",
            "",
            f"Task ID: {task_id}",
            f"Notification ID: {notification['id']}",
            f"Session Key: {session_key}",
            f"Task: {notification['title']}",
        ]

        if metadata.get("parent_project_title"):
            lines.extend([
                "",
                f"Parent project: {metadata['parent_project_title']} ({metadata.get('parent_project_id')})",
            ])

        output_directory = metadata.get("output_directory")
        if output_directory:
            lines.extend([
                "",
                "## Output Directory",
                f"All task artifacts must be written to: `{output_directory}`",
                "- Use descriptive filenames for each artifact.",
                "- Put the primary result in `RESULT.md`.",
            ])
            if self.cyborg_service_url:
                lines.append(
                    f"- Register all output files via the API: POST {self.cyborg_service_url}/api/v1/tasks/{task_id}/files"
                )

        if self.cyborg_service_url:
            lines.extend([
                "",
                "## Instructions",
                f"- If finished, submit now: POST {self.cyborg_service_url}/api/v1/tasks/{task_id}/submit with JSON {{\"result_summary\":\"<answer>\"}}",
                "- If still working, continue and submit when done.",
                "- If stuck, block the task with an input_schema so the operator can help.",
            ])
        else:
            lines.extend([
                "",
                "## Instructions",
                f"- If finished, submit using: cyborg task submit {task_id} --result-summary \"<answer>\".",
                "- If still working, continue and submit when done.",
                "- If stuck, block the task with an input_schema so the operator can help.",
            ])

        return "\n".join(lines)

    def _build_task_tap_agent_params(
        self,
        notification: dict[str, Any],
        route: dict[str, Any],
        session_key: str,
    ) -> dict[str, Any]:
        timeout_seconds = int(max(self.BOOTSTRAP_TIMEOUT_SECONDS, self.settings.timeout_seconds))
        params: dict[str, Any] = {
            "message": self._render_task_tap_prompt(notification, route, session_key),
            "deliver": route.get("channel") is not None,
            "sessionKey": session_key,
            "thinking": "high",
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

    def _render_submission_review_prompt(
        self,
        notification: dict[str, Any],
        route: dict[str, Any],
        session_key: str,
    ) -> str:
        """Render prompt for submission_review notifications."""
        metadata = notification.get("metadata", {})
        task_id = metadata.get("task_id") or notification.get("entity_id")
        otp = metadata.get("submission_review_otp", "")
        result_summary = metadata.get("result_summary", "")

        lines = [
            "## Review Only — Do Not Do Any Work",
            "A task has been submitted for review. Your job is strictly to read what was done and judge if it meets the criteria.",
            "Do NOT write code, create files, run commands, or attempt to fix or complete anything. Only review and approve/reject.",
            "",
            f"Task ID: {task_id}",
            f"Task: {notification['title']}",
        ]

        if metadata.get("parent_project_title"):
            lines.append(f"Parent project: {metadata['parent_project_title']}")

        if result_summary:
            lines.extend(["", f"## What the Agent Claims", result_summary])

        output_directory = metadata.get("output_directory")
        if output_directory:
            lines.extend([
                "",
                "## Output Directory",
                f"Check the files in: `{output_directory}`",
            ])

        lines.extend([
            "",
            "## Review Checklist",
            "1. Does the result actually address the task?",
            "2. Were expected output files created with real content?",
            "3. Is the result substantive, not just a restatement of the plan?",
            "",
            "## Your Action",
            "After reviewing the task output, call the verification command with the one-time password below.",
            "",
            f"**One-time password:** `{otp}`",
            "",
        ])

        if self.cyborg_service_url:
            lines.extend([
                "Approve (if the work is satisfactory):",
                f"  POST {self.cyborg_service_url}/api/v1/tasks/{task_id}/verify-submit",
                f'  {{\"otp\": \"{otp}\", \"approved\": true}}',
                "",
                "Reject (if issues found):",
                f"  POST {self.cyborg_service_url}/api/v1/tasks/{task_id}/verify-submit",
                f'  {{\"otp\": \"{otp}\", \"approved\": false, \"reason\": \"<explain issues>\", \"issues\": [\"<issue1>\"]}}',
            ])
        else:
            lines.extend([
                "Approve (if the work is satisfactory):",
                f"  cyborg task verify-submit {task_id} --otp {otp} --approve",
                "",
                "Reject (if issues found):",
                f"  cyborg task verify-submit {task_id} --otp {otp} --reject --reason \"<explain issues>\"",
            ])

        return "\n".join(lines)

    def _build_submission_review_agent_params(
        self,
        notification: dict[str, Any],
        route: dict[str, Any],
        session_key: str,
    ) -> dict[str, Any]:
        timeout_seconds = int(max(self.BOOTSTRAP_TIMEOUT_SECONDS, self.settings.timeout_seconds))
        params: dict[str, Any] = {
            "message": self._render_submission_review_prompt(notification, route, session_key),
            "deliver": route.get("channel") is not None,
            "sessionKey": session_key,
            "thinking": "high",
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

    def _build_next_action_agent_params(
        self,
        notification: dict[str, Any],
        route: dict[str, Any],
        session_key: str,
    ) -> dict[str, Any]:
        timeout_seconds = int(max(self.BOOTSTRAP_TIMEOUT_SECONDS, self.settings.timeout_seconds))
        params: dict[str, Any] = {
            "message": notification["message"],
            "deliver": True,
            "channel": route["channel"],
            "to": route["to"],
            "sessionKey": session_key,
            "thinking": "high",
            "timeout": timeout_seconds,
            "idempotencyKey": notification["id"],
        }
        if self.settings.agent_id:
            params["agentId"] = self.settings.agent_id
        return params

    def _is_target_task_assignment(self, notification: dict[str, Any]) -> bool:
        metadata = notification.get("metadata", {})
        return (
            notification.get("notification_type") == NotificationType.TASK_ASSIGNMENT.value
            and metadata.get("delivery_route") == "target"
        )

    def _is_agent_dispatch_type(self, notification: dict[str, Any]) -> bool:
        """Check if this notification type dispatches to an agent (not a user)."""
        return notification.get("notification_type") in (
            NotificationType.TASK_ASSIGNMENT.value,
            NotificationType.TASK_RETRY.value,
            NotificationType.TASK_INPUT_RESPONSE.value,
            NotificationType.TASK_TAP.value,
            NotificationType.SUBMISSION_REVIEW.value,
            NotificationType.NEXT_ACTION.value,
        )

    def _is_auto_project_source_task_assignment(self, notification: dict[str, Any]) -> bool:
        metadata = notification.get("metadata", {})
        return (
            notification.get("notification_type") == NotificationType.TASK_ASSIGNMENT.value
            and metadata.get("delivery_route") == "source"
            and bool(metadata.get("auto_created_by_project"))
        )

    def _should_use_task_assignment_agent(self, notification: dict[str, Any], session_key: str | None) -> bool:
        return (
            self._is_target_task_assignment(notification)
            or self._is_auto_project_source_task_assignment(notification)
        ) and bool(session_key)
