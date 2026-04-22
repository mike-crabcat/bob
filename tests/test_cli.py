from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

import cyborg_cli.cli as cli


runner = CliRunner()


class FakeResponse:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False

    def read(self) -> bytes:
        if self.payload is None:
            return b""
        return json.dumps(self.payload).encode()


class FakeTextResponse:
    def __init__(self, payload: str) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeTextResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False

    def read(self) -> bytes:
        return self.payload.encode()


def test_task_list_accepts_raw_list_payload(monkeypatch) -> None:
    def fake_urlopen(_: object, **__: object) -> FakeResponse:
        return FakeResponse(
            [
                {
                    "id": "task-1",
                    "status": "pending",
                    "priority": "medium",
                    "title": "Prep notes for Bob",
                }
            ]
        )

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)

    result = runner.invoke(cli.app, ["task", "list"])

    assert result.exit_code == 0
    assert "Prep notes for Bob" in result.stdout
    assert "pending" in result.stdout


def test_task_retry_sends_empty_json_object(monkeypatch) -> None:
    captured: dict[str, bytes | None] = {}

    def fake_urlopen(request: object, **__: object) -> FakeResponse:
        captured["body"] = getattr(request, "data", None)
        return FakeResponse({"id": "task-1", "title": "Retry sync", "status": "active"})

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)

    result = runner.invoke(cli.app, ["task", "retry", "task-1"])

    assert result.exit_code == 0
    assert captured["body"] == b"{}"
    assert "Task retried: Retry sync" in result.stdout


def test_project_close_sends_empty_json_object(monkeypatch) -> None:
    captured: dict[str, bytes | None] = {}

    def fake_urlopen(request: object, **__: object) -> FakeResponse:
        captured["body"] = getattr(request, "data", None)
        return FakeResponse({"id": "proj-1", "title": "Close me", "state": "closed"})

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)

    result = runner.invoke(cli.app, ["project", "close", "proj-1"])

    assert result.exit_code == 0
    assert captured["body"] == b"{}"
    assert "Project closed: Close me" in result.stdout


def test_project_create_builds_metadata(monkeypatch) -> None:
    captured: dict[str, bytes | None] = {}

    def fake_urlopen(request: object, **__: object) -> FakeResponse:
        captured["body"] = getattr(request, "data", None)
        return FakeResponse({"id": "proj-1", "title": "Proposal", "state": "planning"})

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)

    result = runner.invoke(
        cli.app,
        [
            "project",
            "create",
            "Proposal",
            "--description",
            "Draft the proposal",
            "--session-key",
            "whatsappgroup-main",
            "--channel",
            "whatsapp",
            "--metadata-json",
            '{"source":"cli"}',
        ],
    )

    assert result.exit_code == 0
    payload = json.loads((captured["body"] or b"{}").decode())
    assert payload["metadata"] == {
        "source": "cli",
        "channel": "whatsapp",
        "session_key": "whatsappgroup-main",
    }


def test_project_spec_submit_builds_payload(monkeypatch) -> None:
    captured: dict[str, str | bytes | None] = {}

    def fake_urlopen(request: object, **__: object) -> FakeResponse:
        captured["url"] = getattr(request, "full_url")
        captured["body"] = getattr(request, "data", None)
        return FakeResponse(
            {
                "id": "spec-1",
                "project_id": "proj-1",
                "version_number": 1,
                "aim": "Ship it",
                "method": "Plan and execute",
                "plan": [],
                "success_criteria": [{"check": "x > 0", "description": "Done"}],
                "status": "pending_approval",
                "feedback": None,
                "created_at": "2026-03-18T00:00:00+00:00",
                "approved_at": None,
                "approved_by": None,
                "is_current": False,
            }
        )

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)

    result = runner.invoke(
        cli.app,
        [
            "project",
            "spec",
            "submit",
            "proj-1",
            "--aim",
            "Ship it",
            "--method",
            "Plan and execute",
            "--success-criteria-json",
            '[{"check":"x > 0","description":"Done"}]',
        ],
    )

    assert result.exit_code == 0
    assert captured["url"].endswith("/api/v1/projects/proj-1/specs")
    assert json.loads((captured["body"] or b"{}").decode()) == {
        "aim": "Ship it",
        "method": "Plan and execute",
        "success_criteria": [{"check": "x > 0", "description": "Done"}],
    }


def test_task_update_builds_extended_payload(monkeypatch) -> None:
    captured: dict[str, bytes | None] = {}

    def fake_urlopen(request: object, **__: object) -> FakeResponse:
        captured["body"] = getattr(request, "data", None)
        return FakeResponse({"id": "task-1", "title": "Updated", "status": "pending"})

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)

    result = runner.invoke(
        cli.app,
        [
            "task",
            "update",
            "task-1",
            "--title",
            "Updated",
            "--project-id",
            "proj-1",
            "--project-id",
            "proj-2",
            "--retry-max-attempts",
            "3",
            "--retry-on-failure",
            "retry_from",
            "--retry-from-step",
            "2",
            "--metadata-json",
            '{"source":"cli"}',
            "--channel",
            "telegram",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads((captured["body"] or b"{}").decode())
    assert payload["project_ids"] == ["proj-1", "proj-2"]
    assert payload["retry_config"] == {
        "max_attempts": 3,
        "on_failure": "retry_from",
        "retry_from_step": 2,
    }
    assert payload["metadata"] == {"source": "cli", "channel": "telegram"}


def test_task_update_builds_target_session_metadata(monkeypatch) -> None:
    captured: dict[str, bytes | None] = {}

    def fake_urlopen(request: object, **__: object) -> FakeResponse:
        captured["body"] = getattr(request, "data", None)
        return FakeResponse({"id": "task-1", "title": "Reach out", "status": "planning"})

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)

    result = runner.invoke(
        cli.app,
        [
            "task",
            "update",
            "task-1",
            "--session-key",
            "whatsappgroup-origin",
            "--channel",
            "whatsapp",
            "--target-kind",
            "dm",
            "--target-contact-id",
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads((captured["body"] or b"{}").decode())
    assert payload["metadata"] == {
        "channel": "whatsapp",
        "session_key": "whatsappgroup-origin",
        "target_session": {
            "channel": "whatsapp",
            "kind": "dm",
            "contact_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        },
    }


def test_webhook_create_sends_repeated_events(monkeypatch) -> None:
    captured: dict[str, bytes | None] = {}

    def fake_urlopen(request: object, **__: object) -> FakeResponse:
        captured["body"] = getattr(request, "data", None)
        return FakeResponse({"id": "hook-1", "name": "my-webhook"})

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)

    result = runner.invoke(
        cli.app,
        [
            "webhook",
            "create",
            "my-webhook",
            "--url",
            "https://example.com/webhook",
            "--secret",
            "shh",
            "--event",
            "task.created",
            "--event",
            "task.updated",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads((captured["body"] or b"{}").decode())
    assert payload["events"] == ["task.created", "task.updated"]
    assert "Created webhook: hook-1" in result.stdout


def test_openclaw_context_text_uses_raw_response(monkeypatch) -> None:
    def fake_urlopen(_: object, **__: object) -> FakeTextResponse:
        return FakeTextResponse("plain text context")

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)

    result = runner.invoke(cli.app, ["openclaw", "context"])

    assert result.exit_code == 0
    assert "plain text context" in result.stdout


def test_calendar_create_builds_routing_metadata(monkeypatch) -> None:
    captured: dict[str, bytes | None] = {}

    def fake_urlopen(request: object, **__: object) -> FakeResponse:
        captured["body"] = getattr(request, "data", None)
        return FakeResponse({"id": "cal-1", "name": "Family", "metadata": {"session_key": "whatsappgroup-family"}})

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)

    result = runner.invoke(
        cli.app,
        [
            "calendar",
            "create",
            "Family",
            "--session-key",
            "whatsappgroup-family",
            "--channel",
            "whatsapp",
            "--metadata-json",
            '{"scope":"reminders"}',
        ],
    )

    assert result.exit_code == 0
    payload = json.loads((captured["body"] or b"{}").decode())
    assert payload["metadata"] == {
        "scope": "reminders",
        "channel": "whatsapp",
        "session_key": "whatsappgroup-family",
    }


def test_context_summary_prints_parent_project(monkeypatch) -> None:
    def fake_urlopen(_: object, **__: object) -> FakeResponse:
        return FakeResponse(
            {
                "generated_at": "2026-03-15T00:00:00+00:00",
                "task_counts": {"planning": 1},
                "project_counts": {"planning": 1},
                "active_tasks": [
                    {
                        "id": "task-1",
                        "title": "Draft proposal",
                        "status": "planning",
                        "priority": "critical",
                        "updated_at": "2026-03-15T00:00:00+00:00",
                        "parent_project_id": "proj-1",
                        "parent_project_title": "Proposal work",
                    }
                ],
                "active_projects": [{"id": "proj-1", "title": "Proposal work", "state": "planning", "aim": None}],
                "upcoming_events": [],
            }
        )

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)

    result = runner.invoke(cli.app, ["context", "summary"])

    assert result.exit_code == 0
    assert "project: Proposal work / proj-1" in result.stdout


def test_contact_create_builds_extended_payload(monkeypatch) -> None:
    captured: dict[str, bytes | None] = {}

    def fake_urlopen(request: object, **__: object) -> FakeResponse:
        captured["body"] = getattr(request, "data", None)
        return FakeResponse(
            {
                "id": "contact-1",
                "name": "Alice",
                "phone_number": "+61400111222",
                "email": "alice@example.com",
                "whatsapp_groups": ["family"],
                "metadata": {"source": "cli", "session_key": "whatsappgroup-family"},
            }
        )

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)

    result = runner.invoke(
        cli.app,
        [
            "contact",
            "create",
            "Alice",
            "--phone-number",
            "0400 111 222",
            "--email",
            "alice@example.com",
            "--whatsapp-group",
            "family",
            "--metadata-json",
            '{"source":"cli"}',
            "--session-key",
            "whatsappgroup-family",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads((captured["body"] or b"{}").decode())
    assert payload == {
        "name": "Alice",
        "phone_number": "0400 111 222",
        "email": "alice@example.com",
        "whatsapp_groups": ["family"],
        "metadata": {
            "source": "cli",
            "session_key": "whatsappgroup-family",
        },
    }
    assert "Created contact: contact-1" in result.stdout


def test_contact_update_can_clear_whatsapp_groups(monkeypatch) -> None:
    captured: dict[str, bytes | None] = {}

    def fake_urlopen(request: object, **__: object) -> FakeResponse:
        captured["body"] = getattr(request, "data", None)
        return FakeResponse(
            {
                "id": "contact-1",
                "name": "Alice",
                "phone_number": "+61400111222",
                "whatsapp_groups": [],
                "metadata": {},
            }
        )

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)

    result = runner.invoke(cli.app, ["contact", "update", "contact-1", "--clear-whatsapp-groups"])

    assert result.exit_code == 0
    payload = json.loads((captured["body"] or b"{}").decode())
    assert payload["whatsapp_groups"] == []


def test_contact_by_whatsapp_group_prints_table(monkeypatch) -> None:
    def fake_urlopen(_: object, **__: object) -> FakeResponse:
        return FakeResponse(
            [
                {
                    "id": "contact-1",
                    "name": "Alice",
                    "phone_number": "+61400111222",
                }
            ]
        )

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)

    result = runner.invoke(cli.app, ["contact", "by-whatsapp-group", "family"])

    assert result.exit_code == 0
    assert "Alice" in result.stdout
    assert "+61400111222" in result.stdout


def test_contact_by_email_url_encodes_lookup_value(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_urlopen(request: object, **__: object) -> FakeResponse:
        captured["url"] = getattr(request, "full_url")
        return FakeResponse(
            {
                "id": "contact-1",
                "name": "Alice",
                "phone_number": "+61400111222",
            }
        )

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)

    result = runner.invoke(cli.app, ["contact", "by-email", "alice+family@example.com"])

    assert result.exit_code == 0
    assert captured["url"].endswith("/api/v1/contacts/by-email/alice%2Bfamily%40example.com")


def test_notification_list_builds_filters(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_urlopen(request: object, **__: object) -> FakeResponse:
        captured["url"] = getattr(request, "full_url")
        return FakeResponse(
            [
                {
                    "id": "note-1",
                    "entity_type": "task",
                    "entity_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "notification_type": "needs_input",
                    "status": "pending",
                    "title": "Task needs input",
                    "message": "Approve the plan",
                    "metadata": {},
                    "sequence_number": 1,
                    "created_at": "2026-03-15T00:00:00+00:00",
                    "updated_at": "2026-03-15T00:00:00+00:00",
                    "acknowledged_at": None,
                    "acknowledged_by": None,
                    "resolved_at": None,
                    "source_updated_at": None,
                }
            ]
        )

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)

    result = runner.invoke(
        cli.app,
        ["notification", "list", "--status", "pending", "--entity-type", "task", "--limit", "5"],
    )

    assert result.exit_code == 0
    assert "Task needs input" in result.stdout
    assert captured["url"].endswith("/api/v1/notifications?status=pending&entity_type=task&limit=5")


def test_notification_ack_sends_acknowledged_by(monkeypatch) -> None:
    captured: dict[str, bytes | None] = {}

    def fake_urlopen(request: object, **__: object) -> FakeResponse:
        captured["body"] = getattr(request, "data", None)
        return FakeResponse({"id": "note-1", "status": "acknowledged"})

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)

    result = runner.invoke(
        cli.app,
        ["notification", "ack", "note-1", "--acknowledged-by", "mobile-client"],
    )

    assert result.exit_code == 0
    assert json.loads((captured["body"] or b"{}").decode()) == {"acknowledged_by": "mobile-client"}
    assert "Acknowledged notification: note-1" in result.stdout


def test_notification_process_due_calls_endpoint(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request: object, **__: object) -> FakeResponse:
        captured["url"] = getattr(request, "full_url")
        captured["body"] = getattr(request, "data", None)
        return FakeResponse({"processed": 3})

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)

    result = runner.invoke(cli.app, ["notification", "process-due"])

    assert result.exit_code == 0
    assert captured["url"].endswith("/api/v1/notifications/process-due")
    assert captured["body"] == b"{}"
    assert "Processed notifications: 3" in result.stdout


def test_serve_loads_openclaw_settings_from_config_dir_env_file(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for key in (
        "CYBORG_OPENCLAW_BASE_URL",
        "CYBORG_OPENCLAW_TOKEN",
        "CYBORG_OPENCLAW_HOOK_PATH",
        "CYBORG_NOTIFICATION_DISPATCH_INTERVAL_SECONDS",
        "CYBORG_HEARTBEAT_INTERVAL_SECONDS",
        "CYBORG_ENV_FILE",
    ):
        monkeypatch.delenv(key, raising=False)
    env_file = config_dir / ".env"
    env_file.write_text(
        "\n".join(
            [
                "CYBORG_OPENCLAW_BASE_URL=https://openclaw.example",
                "CYBORG_OPENCLAW_TOKEN=secret-token",
                "CYBORG_OPENCLAW_HOOK_PATH=/hooks/agent",
                "CYBORG_NOTIFICATION_DISPATCH_INTERVAL_SECONDS=15",
            ]
        ),
        encoding="utf-8",
    )

    def fake_create_app(settings: object) -> object:
        captured["settings"] = settings
        return "fake-app"

    def fake_uvicorn_run(app: object, *, host: str, port: int, log_level: str) -> None:
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port
        captured["log_level"] = log_level

    monkeypatch.setattr(cli, "create_app", fake_create_app)
    monkeypatch.setattr(cli.uvicorn, "run", fake_uvicorn_run)

    cli.serve(
        host="127.0.0.1",
        port=8420,
        data_dir=tmp_path / "data",
        config_dir=config_dir,
        db_path=tmp_path / "data" / "cyborg.db",
        log_level="info",
    )

    settings = captured["settings"]
    assert settings.openclaw.base_url == "https://openclaw.example"
    assert settings.openclaw.token == "secret-token"
    assert settings.openclaw.hook_path == "/hooks/agent"
    assert settings.heartbeat_interval_seconds == 15.0
    assert captured["app"] == "fake-app"
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8420
    assert captured["log_level"] == "info"


def test_service_file_contents_exports_config_dir() -> None:
    settings = cli.Settings(
        data_dir=Path("/tmp/data"),
        config_dir=Path("/tmp/config"),
        db_path=Path("/tmp/data/cyborg.db"),
    )

    service_file = cli._service_file_contents(settings, Path("/work"))

    assert "Environment=CYBORG_CONFIG_DIR=/tmp/config" in service_file


def test_session_route_create_builds_payload(monkeypatch) -> None:
    captured: dict[str, bytes | None] = {}

    def fake_urlopen(request: object, **__: object) -> FakeResponse:
        captured["body"] = getattr(request, "data", None)
        return FakeResponse(
            {
                "id": "route-1",
                "channel": "whatsapp",
                "session_key": "whatsappgroup-family",
                "kind": "group",
                "chat_id": "120363426096069246@g.us",
                "contact_id": None,
                "metadata": {"scope": "alerts"},
                "is_active": True,
                "created_at": "2026-03-15T00:00:00+00:00",
                "updated_at": "2026-03-15T00:00:00+00:00",
                "deleted_at": None,
            }
        )

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)

    result = runner.invoke(
        cli.app,
        [
            "session-route",
            "create",
            "whatsappgroup-family",
            "--kind",
            "group",
            "--chat-id",
            "120363426096069246@g.us",
            "--metadata-json",
            '{"scope":"alerts"}',
        ],
    )

    assert result.exit_code == 0
    assert json.loads((captured["body"] or b"{}").decode()) == {
        "session_key": "whatsappgroup-family",
        "channel": "whatsapp",
        "kind": "group",
        "chat_id": "120363426096069246@g.us",
        "metadata": {"scope": "alerts"},
    }
    assert "Created session route: route-1" in result.stdout
