from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import socket
import threading
import time
from typing import Any
from uuid import uuid4

import httpx
import pytest
import uvicorn

from cyborg.config import OpenClawHookSettings, Settings
from cyborg.database import Database
from cyborg.main import create_app
from cyborg.models import (
    JournalEntryType,
    ProjectCloseRequest,
    ProjectCreate,
    ProjectJournalEntryCreate,
    ProjectSpecApproveRequest,
    TaskBlockRequest,
    TaskCreate,
    TaskFailureRequest,
)
from cyborg.services.notification_service import NotificationService
from cyborg.services.openclaw_hook_service import OpenClawHookService
from cyborg.services.openclaw_reasoning_service import OpenClawReasoningService
from cyborg.services.project_service import ProjectService
from cyborg.services.project_spec_service import ProjectSpecService
from cyborg.services.session_route_service import SessionRouteService
from cyborg.services.task_service import TaskService


SCHEMA_DIR = Path(__file__).resolve().parents[2] / "cyborg" / "schemas"
TASK_ASSIGNMENT_TIMEOUT_SECONDS = 90.0
CHAT_POLL_INTERVAL_SECONDS = 2.0


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--openclaw-live",
        action="store_true",
        default=False,
        help="Run the live OpenClaw acceptance suite against a real gateway and model.",
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _env_truthy(name: str) -> bool:
    value = os.getenv(name, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _safe_nodeid(nodeid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", nodeid).strip("_") or "openclaw_acceptance"


def _extract_text_blocks(message: dict[str, Any]) -> list[str]:
    content = message.get("content")
    if isinstance(content, str):
        stripped = content.strip()
        return [stripped] if stripped else []
    if not isinstance(content, list):
        return []
    blocks: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "text":
            continue
        text = str(item.get("text", "")).strip()
        if text:
            blocks.append(text)
    return blocks


def _message_text(message: dict[str, Any]) -> str:
    return "\n".join(_extract_text_blocks(message)).strip()


def _pick(value: str | None, *fallbacks: str | None) -> str:
    candidates = (value, *fallbacks)
    for candidate in candidates:
        if candidate and candidate.strip():
            return candidate.strip()
    return ""


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


@dataclass(slots=True)
class OpenClawLiveConfig:
    gateway_url: str
    gateway_token: str
    agent_id: str
    session_prefix: str


class UvicornThread(threading.Thread):
    def __init__(self, app: Any, host: str, port: int) -> None:
        super().__init__(daemon=True)
        self.config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
        self.server = uvicorn.Server(self.config)

    def run(self) -> None:
        self.server.run()

    def stop(self) -> None:
        self.server.should_exit = True


class AcceptanceBuilder:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.project_service = ProjectService(db)
        self.project_spec_service = ProjectSpecService(db)
        self.task_service = TaskService(db)
        self.notification_service = NotificationService(db)

    def create_contact(
        self,
        *,
        name: str,
        phone_number: str,
        email: str | None = None,
        whatsapp_groups: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        contact_id = str(uuid4())
        now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        _run(
            self.db.execute(
                """
                INSERT INTO contacts (
                    id, name, phone_number, email, whatsapp_groups, metadata, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contact_id,
                    name,
                    phone_number,
                    email,
                    json.dumps(whatsapp_groups or []),
                    json.dumps(metadata or {}),
                    now,
                    now,
                ),
            )
        )
        row = _run(self.db.fetch_one("SELECT * FROM contacts WHERE id = ?", (contact_id,)))
        if row is None:
            raise AssertionError(f"Failed to create contact {contact_id}")
        row["whatsapp_groups"] = json.loads(row["whatsapp_groups"]) if row.get("whatsapp_groups") else []
        row["metadata"] = json.loads(row["metadata"]) if row.get("metadata") else {}
        return row

    def create_project(
        self,
        *,
        title: str,
        description: str | None = None,
        aim: str | None = None,
        method: str | None = None,
        success_criteria: list[dict[str, Any]] | None = None,
        plan: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        auto_execute: bool = True,
        approve_spec: bool = False,
        close_conclusion: str | None = None,
    ) -> dict[str, Any]:
        project = _run(
            self.project_service.create_project(
                ProjectCreate(
                    title=title,
                    description=description,
                    aim=aim,
                    method=method,
                    success_criteria=success_criteria or [],
                    plan=plan or [],
                    metadata=metadata or {},
                    auto_execute=auto_execute,
                )
            )
        )
        project_id = str(project.id)
        if approve_spec:
            specs = _run(self.project_spec_service.list_specs(project_id))
            if not specs.specs:
                raise AssertionError(f"Project {project_id} has no project specs to approve")
            _run(
                self.project_spec_service.approve_spec(
                    str(specs.specs[0].id),
                    ProjectSpecApproveRequest(approver="OpenClaw acceptance"),
                )
            )
        if close_conclusion is not None:
            project = _run(
                self.project_service.close_project(
                    project_id,
                    ProjectCloseRequest(conclusion=close_conclusion),
                )
            )
        return project.model_dump(mode="json")

    def add_project_journal_entry(
        self,
        project_id: str,
        *,
        entry_type: JournalEntryType,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        entry = _run(
            self.project_service.add_journal_entry(
                project_id,
                ProjectJournalEntryCreate(
                    entry_type=entry_type,
                    content=content,
                    metadata=metadata or {},
                ),
            )
        )
        return entry.model_dump(mode="json")

    def create_task(
        self,
        *,
        title: str,
        description: str | None = None,
        plan: str,
        requested_by: str | None = None,
        project_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        approve_plan: bool = False,  # Deprecated: tasks now start in pending
        start: bool = False,
        complete_result: str | None = None,
        fail_result: str | None = None,
    ) -> dict[str, Any]:
        task = _run(
            self.task_service.create_task(
                TaskCreate(
                    title=title,
                    description=description,
                    requested_by=requested_by,
                    plan=plan,
                    project_ids=project_ids or [],
                    metadata=metadata or {},
                )
            )
        )
        task_id = str(task.id)
        if start:
            _run(self.task_service.start_task(task_id))
        if complete_result is not None:
            _run(self.task_service.complete_task(task_id, complete_result))
        if fail_result is not None:
            _run(self.task_service.fail_task(task_id, TaskFailureRequest(result=fail_result)))
        return self.get_task(task_id)

    def block_task(self, task_id: str, *, reason: str, resume_instructions: str) -> dict[str, Any]:
        task = _run(
            self.task_service.block_task(
                task_id,
                TaskBlockRequest(reason=reason, resume_instructions=resume_instructions),
            )
        )
        return task.model_dump(mode="json")

    def approve_initial_plan(self, task_id: str) -> None:
        # Deprecated: tasks now start in pending status, no plan approval needed
        pass

    def start_task(self, task_id: str) -> dict[str, Any]:
        task = _run(self.task_service.start_task(task_id))
        return task.model_dump(mode="json")

    def complete_task(self, task_id: str, result_summary: str) -> dict[str, Any]:
        task = _run(self.task_service.complete_task(task_id, result_summary))
        return task.model_dump(mode="json")

    def get_task(self, task_id: str) -> dict[str, Any]:
        task = _run(self.task_service.get_task(task_id))
        return task.model_dump(mode="json")

    def get_project(self, project_id: str) -> dict[str, Any]:
        project = _run(self.project_service.get_project(project_id))
        return project.model_dump(mode="json")

    def close_project(self, project_id: str, *, conclusion: str) -> dict[str, Any]:
        project = _run(
            self.project_service.close_project(
                project_id,
                ProjectCloseRequest(conclusion=conclusion),
            )
        )
        return project.model_dump(mode="json")

    def list_notifications(self) -> list[dict[str, Any]]:
        notifications = _run(self.notification_service.list_notifications(limit=100))
        return [notification.model_dump(mode="json") for notification in notifications]

    def get_task_assignment_notification(self, task_id: str) -> dict[str, Any]:
        notifications = self.list_notifications()
        for notification in notifications:
            if notification["entity_id"] == task_id and notification["notification_type"] == "task_assignment":
                return notification
        raise AssertionError(f"No pending task_assignment notification found for task {task_id}")


class OpenClawLiveHarness:
    def __init__(self, db: Database, config: OpenClawLiveConfig, artifact_dir: Path) -> None:
        self.db = db
        self.config = config
        self.artifact_dir = artifact_dir
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self._call_index = 0

    def make_hook_service(self, *, cyborg_service_url: str | None = None) -> OpenClawHookService:
        return OpenClawHookService(self.db, routing_service=SessionRouteService(self.db), cyborg_service_url=cyborg_service_url)

    def write_artifact(self, name: str, payload: Any) -> None:
        target = self.artifact_dir / name
        if isinstance(payload, str):
            target.write_text(payload, encoding="utf-8")
            return
        target.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")

    def call_gateway(
        self,
        method: str,
        params: dict[str, Any],
        *,
        expect_final: bool = False,
        timeout_seconds: float | None = None,
        cyborg_service_url: str | None = None,
    ) -> dict[str, Any]:
        self._call_index += 1
        prefix = f"{self._call_index:02d}_{method.replace('.', '_')}"
        self.write_artifact(f"{prefix}_request.json", {"method": method, "params": params, "expect_final": expect_final})
        response = _run(
            self.make_hook_service(cyborg_service_url=cyborg_service_url)._send_gateway_request(
                method,
                params,
                expect_final=expect_final,
                timeout_seconds=timeout_seconds,
            )
        )
        self.write_artifact(f"{prefix}_response.json", response)
        return response

    def new_session_key(self, purpose: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", purpose.lower()).strip("-") or "session"
        return f"{self.config.session_prefix}:{slug}:{uuid4()}"

    def fetch_history(self, session_key: str, *, limit: int = 50) -> dict[str, Any]:
        history = self.call_gateway(
            "chat.history",
            {"sessionKey": session_key, "limit": limit},
        )
        self.write_artifact(f"history_{_safe_nodeid(session_key)}.json", history)
        return history

    def send_chat(self, session_key: str, message: str, *, idempotency_key: str | None = None) -> dict[str, Any]:
        return self.call_gateway(
            "chat.send",
            {
                "sessionKey": session_key,
                "message": message,
                "idempotencyKey": idempotency_key or f"chat-send-{uuid4()}",
            },
        )

    def run_agent(
        self,
        *,
        session_key: str,
        message: str,
        thinking: str = "off",
        timeout_seconds: float = 60.0,
        idempotency_key: str | None = None,
        deliver: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "message": message,
            "deliver": deliver,
            "sessionKey": session_key,
            "thinking": thinking,
            "timeout": int(timeout_seconds),
            "idempotencyKey": idempotency_key or f"agent-run-{uuid4()}",
        }
        if self.config.agent_id:
            params["agentId"] = self.config.agent_id
        return self.call_gateway(
            "agent",
            params,
            expect_final=True,
            timeout_seconds=timeout_seconds,
        )

    def response_text(self, response: dict[str, Any], *, session_key: str | None = None) -> str:
        result = response.get("result")
        if isinstance(result, dict):
            payloads = result.get("payloads")
            if isinstance(payloads, list):
                payload_text = "\n".join(
                    str(item.get("text", "")).strip()
                    for item in payloads
                    if isinstance(item, dict) and str(item.get("text", "")).strip()
                ).strip()
                if payload_text:
                    return payload_text
            for key in ("content", "text", "message"):
                value = result.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        payloads = response.get("payloads")
        if isinstance(payloads, list):
            payload_text = "\n".join(
                str(item.get("text", "")).strip()
                for item in payloads
                if isinstance(item, dict) and str(item.get("text", "")).strip()
            ).strip()
            if payload_text:
                return payload_text
        for key in ("content", "text", "message", "summary"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        if session_key:
            history = self.fetch_history(session_key)
            for message in reversed(history.get("messages", [])):
                if message.get("role") != "assistant":
                    continue
                text = _message_text(message)
                if text:
                    return text
        return ""

    def wait_for_assistant_reply(
        self,
        session_key: str,
        *,
        previous_assistant_count: int = 0,
        timeout_seconds: float = TASK_ASSIGNMENT_TIMEOUT_SECONDS,
    ) -> tuple[str, dict[str, Any]]:
        deadline = time.monotonic() + timeout_seconds
        last_history: dict[str, Any] | None = None
        last_error: str | None = None
        while time.monotonic() < deadline:
            history = self.fetch_history(session_key)
            last_history = history
            assistant_messages = [m for m in history.get("messages", []) if m.get("role") == "assistant"]
            if len(assistant_messages) > previous_assistant_count:
                latest = assistant_messages[-1]
                text = _message_text(latest)
                if text:
                    return text, history
                if latest.get("errorMessage"):
                    last_error = str(latest["errorMessage"])
            time.sleep(CHAT_POLL_INTERVAL_SECONDS)
        details = {"session_key": session_key, "last_history": last_history, "last_error": last_error}
        self.write_artifact(f"timeout_{_safe_nodeid(session_key)}.json", details)
        raise AssertionError(f"Timed out waiting for assistant reply in session {session_key}: {last_error or 'no assistant text produced'}")

    def assistant_message_count(self, history: dict[str, Any]) -> int:
        return sum(1 for message in history.get("messages", []) if message.get("role") == "assistant")

    def resolve_target_route(self, notification: dict[str, Any]) -> tuple[dict[str, Any], str]:
        hook_service = self.make_hook_service()
        route = _run(hook_service.routing_service.resolve_notification_route(notification.get("metadata", {})))
        if route is None:
            raise AssertionError(f"Could not resolve route for notification {notification['id']}")
        session_key = _run(hook_service.routing_service.resolve_target_session_key(notification.get("metadata", {})))
        if not session_key:
            raise AssertionError(f"Could not resolve target session key for notification {notification['id']}")
        return route.model_dump(mode="json"), session_key


@pytest.fixture(scope="session")
def openclaw_live_config(pytestconfig: pytest.Config) -> OpenClawLiveConfig:
    if not (pytestconfig.getoption("--openclaw-live") or _env_truthy("OPENCLAW_ACCEPTANCE")):
        pytest.skip("OpenClaw live acceptance suite disabled. Use --openclaw-live or OPENCLAW_ACCEPTANCE=1.")

    gateway_url = _pick(
        os.getenv("OPENCLAW_ACCEPTANCE_GATEWAY_URL"),
        os.getenv("CYBORG_OPENCLAW_GATEWAY_URL"),
        os.getenv("CYBORG_OPENCLAW_BASE_URL"),
    )
    gateway_token = _pick(
        os.getenv("OPENCLAW_ACCEPTANCE_GATEWAY_TOKEN"),
        os.getenv("CYBORG_OPENCLAW_GATEWAY_TOKEN"),
        os.getenv("CYBORG_OPENCLAW_TOKEN"),
    )
    if gateway_url.startswith("http://"):
        gateway_url = "ws://" + gateway_url[len("http://") :]
    elif gateway_url.startswith("https://"):
        gateway_url = "wss://" + gateway_url[len("https://") :]
    if not gateway_url or not gateway_token:
        pytest.fail(
            "OpenClaw live acceptance is enabled but gateway credentials are missing. "
            "Set OPENCLAW_ACCEPTANCE_GATEWAY_URL and OPENCLAW_ACCEPTANCE_GATEWAY_TOKEN "
            "(or the equivalent CYBORG_OPENCLAW_* variables)."
        )

    agent_id = _pick(os.getenv("OPENCLAW_ACCEPTANCE_AGENT_ID"), os.getenv("CYBORG_OPENCLAW_AGENT_ID"), "main")
    session_prefix = _pick(os.getenv("OPENCLAW_ACCEPTANCE_SESSION_PREFIX"), "acceptance")
    return OpenClawLiveConfig(
        gateway_url=gateway_url,
        gateway_token=gateway_token,
        agent_id=agent_id,
        session_prefix=session_prefix,
    )


@pytest.fixture
def artifact_dir(pytestconfig: pytest.Config, request: pytest.FixtureRequest) -> Path:
    base = Path(str(pytestconfig.cache.mkdir("openclaw_acceptance")))
    target = base / _safe_nodeid(request.node.nodeid)
    target.mkdir(parents=True, exist_ok=True)
    return target


@pytest.fixture
def acceptance_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        db_path=tmp_path / "data" / "cyborg.db",
    )


@pytest.fixture
def acceptance_db(acceptance_settings: Settings) -> Database:
    db = Database(acceptance_settings.db_path, SCHEMA_DIR, pool_size=1)
    db.settings = acceptance_settings
    _run(db.connect())
    _run(db.apply_migrations())
    try:
        yield db
    finally:
        _run(db.close())


@pytest.fixture
def openclaw_gateway_db(acceptance_settings: Settings, openclaw_live_config: OpenClawLiveConfig) -> Database:
    gateway_settings = Settings(
        data_dir=acceptance_settings.data_dir,
        config_dir=acceptance_settings.config_dir,
        db_path=acceptance_settings.db_path,
        openclaw=OpenClawHookSettings(
            gateway_url=openclaw_live_config.gateway_url,
            gateway_token=openclaw_live_config.gateway_token,
            agent_id=openclaw_live_config.agent_id,
        ),
    )
    db = Database(gateway_settings.db_path, SCHEMA_DIR, pool_size=1)
    db.settings = gateway_settings
    _run(db.connect())
    _run(db.apply_migrations())
    hook_service = OpenClawHookService(db)
    try:
        _run(
            hook_service._send_gateway_request(
                "chat.history",
                {"sessionKey": f"{openclaw_live_config.session_prefix}:probe:{uuid4()}", "limit": 1},
            )
        )
    except Exception as exc:
        _run(db.close())
        pytest.fail(f"OpenClaw live acceptance could not reach the gateway: {exc}")
    try:
        yield db
    finally:
        _run(db.close())


@pytest.fixture
def acceptance_builder(acceptance_db: Database) -> AcceptanceBuilder:
    return AcceptanceBuilder(acceptance_db)


@pytest.fixture
def live_openclaw(openclaw_gateway_db: Database, openclaw_live_config: OpenClawLiveConfig, artifact_dir: Path) -> OpenClawLiveHarness:
    return OpenClawLiveHarness(openclaw_gateway_db, openclaw_live_config, artifact_dir)


@pytest.fixture
def live_reasoning_service(openclaw_gateway_db: Database) -> OpenClawReasoningService:
    return OpenClawReasoningService(openclaw_gateway_db)


@pytest.fixture
def cyborg_http_server(acceptance_settings: Settings, artifact_dir: Path) -> str:
    host = "127.0.0.1"
    port = _available_port()
    app = create_app(acceptance_settings)
    server = UvicornThread(app, host=host, port=port)
    server.start()
    base_url = f"http://{host}:{port}"
    client = httpx.Client(base_url=base_url, timeout=5.0)
    started = False
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                response = client.get("/health")
                if response.status_code == 200:
                    started = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        if not started:
            raise RuntimeError(f"Cyborg HTTP server failed to start on {base_url}")
        artifact = {"base_url": base_url, "port": port}
        (artifact_dir / "cyborg_server.json").write_text(json.dumps(artifact, indent=2), encoding="utf-8")
        yield base_url
    finally:
        client.close()
        server.stop()
        server.join(timeout=10)
