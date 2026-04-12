"""Direct OpenClaw gateway delivery for Cyborg notifications."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import shutil
from typing import Any
from uuid import uuid4

from cyborg import __version__
from cyborg.config import OpenClawHookSettings, Settings
from cyborg.database import Database
from cyborg.models import NotificationDeliveryStatus, NotificationType
from cyborg.services.base import BaseService, utcnow
from cyborg.services.prompt_history import log_prompt
from cyborg.services.session_route_service import SessionRouteService


class OpenClawHookService(BaseService):
    """Send Cyborg notifications to OpenClaw via the gateway RPC surface."""

    MAX_RETRY_DELAY = timedelta(hours=6)
    GATEWAY_PROTOCOL_VERSION = 3
    GATEWAY_CLIENT_ID = "gateway-client"
    GATEWAY_CLIENT_MODE = "backend"
    GATEWAY_SCOPES = ["operator.write"]
    BOOTSTRAP_TIMEOUT_SECONDS = 180.0

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
        route = await self.routing_service.resolve_notification_route(notification.get("metadata", {}))
        if route is None:
            raise ValueError("No delivery route could be resolved for the notification")

        route_data = route.model_dump(mode="json")
        delivery_session_key = await self._resolve_delivery_session_key(notification, route_data)

        # Task assignments and plan approvals use the agent method with detailed prompt
        if self._should_use_task_assignment_agent(notification, delivery_session_key):
            if not is_retry:
                metadata = notification.get("metadata", {})
                await log_prompt(
                    self.db,
                    category="task_assignment",
                    prompt_text=self._render_task_assignment_prompt(notification, route_data, delivery_session_key),
                    project_id=metadata.get("parent_project_id") or metadata.get("project_id"),
                    task_id=metadata.get("task_id") or notification.get("entity_id"),
                    session_key=delivery_session_key,
                )
            await self._send_gateway_request(
                "agent",
                self._build_task_assignment_agent_params(notification, route_data, delivery_session_key),
                expect_final=True,
                timeout_seconds=self.BOOTSTRAP_TIMEOUT_SECONDS,
            )
            return

        # Needs input notifications (plan approvals, etc.) also use agent method for context
        if notification.get("notification_type") == "needs_input":
            session_key = delivery_session_key or self._resolve_visible_session_key(route_data)
            if not is_retry:
                metadata = notification.get("metadata", {})
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
                expect_final=True,
                timeout_seconds=self.BOOTSTRAP_TIMEOUT_SECONDS,
            )
            return

        # Task retry notifications use the agent method with retry-specific prompt
        if notification.get("notification_type") == NotificationType.TASK_RETRY.value:
            session_key = delivery_session_key or self._resolve_visible_session_key(route_data)
            if not is_retry:
                metadata = notification.get("metadata", {})
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
                expect_final=True,
                timeout_seconds=self.BOOTSTRAP_TIMEOUT_SECONDS,
            )
            return

        # Task input response notifications use the agent method with input-specific prompt
        if notification.get("notification_type") == NotificationType.TASK_INPUT_RESPONSE.value:
            session_key = delivery_session_key or self._resolve_visible_session_key(route_data)
            if not is_retry:
                metadata = notification.get("metadata", {})
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
                expect_final=True,
                timeout_seconds=self.BOOTSTRAP_TIMEOUT_SECONDS,
            )
            return

        # Task tap notifications nudge the agent to continue or submit
        if notification.get("notification_type") == NotificationType.TASK_TAP.value:
            session_key = delivery_session_key or self._resolve_visible_session_key(route_data)
            if not is_retry:
                metadata = notification.get("metadata", {})
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
                expect_final=True,
                timeout_seconds=self.BOOTSTRAP_TIMEOUT_SECONDS,
            )
            return

        # Submission review notifications ask the agent to verify its own work
        if notification.get("notification_type") == NotificationType.SUBMISSION_REVIEW.value:
            session_key = delivery_session_key or self._resolve_visible_session_key(route_data)
            if not is_retry:
                metadata = notification.get("metadata", {})
                await log_prompt(
                    self.db,
                    category="submission_review",
                    prompt_text=self._render_submission_review_prompt(notification, route_data, session_key),
                    project_id=metadata.get("parent_project_id") or metadata.get("project_id"),
                    task_id=metadata.get("task_id") or notification.get("entity_id"),
                    session_key=session_key,
                )
            await self._send_gateway_request(
                "agent",
                self._build_submission_review_agent_params(notification, route_data, session_key),
                expect_final=True,
                timeout_seconds=self.BOOTSTRAP_TIMEOUT_SECONDS,
            )
            return

        visible_session_key = delivery_session_key or self._resolve_visible_session_key(route_data)

        if not is_retry:
            metadata = notification.get("metadata", {})
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
        if notification_type in ("task_assignment", "needs_input", "task_retry", "task_tap"):
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
        openclaw_bin = shutil.which("openclaw")
        if openclaw_bin:
            return await self._send_gateway_request_via_cli(
                openclaw_bin,
                method,
                params,
                expect_final=expect_final,
                timeout_seconds=timeout_seconds,
            )
        return await self._send_gateway_request_via_websocket(
            method,
            params,
            expect_final=expect_final,
            timeout_seconds=timeout_seconds,
        )

    async def _send_gateway_request_via_cli(
        self,
        openclaw_bin: str,
        method: str,
        params: dict[str, Any],
        *,
        expect_final: bool,
        timeout_seconds: float | None,
    ) -> dict[str, Any]:
        timeout = timeout_seconds or self.settings.timeout_seconds
        command = [
            openclaw_bin,
            "gateway",
            "call",
            method,
            "--json",
            "--params",
            json.dumps(params),
            "--timeout",
            str(int(timeout * 1000)),
        ]
        if expect_final:
            command.append("--expect-final")
        gateway_url = self.settings.resolved_gateway_url
        gateway_token = self.settings.resolved_gateway_token
        if gateway_url:
            command.extend(["--url", gateway_url])
        if gateway_token:
            command.extend(["--token", gateway_token])

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout + 5,
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise RuntimeError(f"OpenClaw gateway CLI timed out calling {method}") from exc

        if process.returncode != 0:
            error_text = stderr.decode().strip() or stdout.decode().strip() or f"exit {process.returncode}"
            raise RuntimeError(f"OpenClaw gateway CLI failed calling {method}: {error_text}")

        output = stdout.decode().strip()
        if not output:
            return {}
        payload = json.loads(output)
        return payload if isinstance(payload, dict) else {"payload": payload}

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
            await self._await_gateway_challenge(websocket, timeout_seconds=timeout)

            connect_params = self._build_gateway_connect_params()
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

    async def _await_gateway_challenge(self, websocket: Any, *, timeout_seconds: float) -> None:
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
            return None

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

    def _build_task_assignment_agent_params(
        self,
        notification: dict[str, Any],
        route: dict[str, Any],
        session_key: str,
    ) -> dict[str, Any]:
        timeout_seconds = int(max(self.BOOTSTRAP_TIMEOUT_SECONDS, self.settings.timeout_seconds))
        params: dict[str, Any] = {
            "message": self._render_task_assignment_prompt(notification, route, session_key),
            "deliver": True,
            "channel": route["channel"],
            "to": route["to"],
            "sessionKey": session_key,
            "thinking": "low" if notification.get("metadata", {}).get("auto_created_by_project") else "off",
            "timeout": timeout_seconds,
            "idempotencyKey": notification["id"],
        }
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
            "thinking": "low",
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

    def _render_task_assignment_prompt(
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
                "",
                "Task brief:",
                notification["message"],
            ]
        )
        if metadata.get("parent_project_title"):
            lines.extend(
                [
                    "",
                    f"Parent project: {metadata['parent_project_title']} ({metadata.get('parent_project_id')})",
                ]
            )
        if metadata.get("requested_by"):
            lines.extend(["", f"Requested by: {metadata['requested_by']}"])
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
            "deliver": True,
            "channel": route["channel"],
            "to": route["to"],
            "sessionKey": session_key,
            "thinking": "low",
            "timeout": timeout_seconds,
            "idempotencyKey": notification["id"],
        }
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
            "deliver": True,
            "channel": route["channel"],
            "to": route["to"],
            "sessionKey": session_key,
            "thinking": "low",
            "timeout": timeout_seconds,
            "idempotencyKey": notification["id"],
        }
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
            "deliver": True,
            "channel": route["channel"],
            "to": route["to"],
            "sessionKey": session_key,
            "thinking": "low",
            "timeout": timeout_seconds,
            "idempotencyKey": notification["id"],
        }
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
            "A task has been submitted for your review. You must verify whether the work was actually completed.",
            "Be strict but fair. The agent should have done real work, not just restated the plan.",
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
            "deliver": True,
            "channel": route["channel"],
            "to": route["to"],
            "sessionKey": session_key,
            "thinking": "low",
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
