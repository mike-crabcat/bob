"""Typer CLI for running and managing Cyborg."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import shutil
import shlex
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import qrcode
import typer
import uvicorn

from cyborg_server.config import DEFAULT_HOST, DEFAULT_PORT, Settings
from cyborg_server.main import create_app


SERVICE_NAME = "cyborg.service"
app = typer.Typer(help="Cyborg - Bob's memory and planning service.")

task_app = typer.Typer(help="Task operations")
plan_app = typer.Typer(help="Plan operations for tasks")
project_app = typer.Typer(help="Project operations")
project_spec_app = typer.Typer(help="Spec operations for projects")
contact_app = typer.Typer(help="Contact operations")
notification_app = typer.Typer(help="Notification operations")
session_route_app = typer.Typer(help="Session route registry operations")
calendar_app = typer.Typer(help="Calendar operations")
event_app = typer.Typer(help="Event operations")
context_app = typer.Typer(help="Context operations")
webhook_app = typer.Typer(help="Webhook operations")
openclaw_app = typer.Typer(help="OpenClaw integration operations")
planning_app = typer.Typer(help="AI-powered project planning")
health_app = typer.Typer(help="Project health monitoring")
learning_app = typer.Typer(help="Project insights and learning")
phone_app = typer.Typer(help="Phone call operations")
openai_app = typer.Typer(help="OpenAI LLM evaluation commands")
eval_app = typer.Typer(help="LLM eval framework")

app.add_typer(task_app, name="task")
task_app.add_typer(plan_app, name="plan")
app.add_typer(project_app, name="project")
project_app.add_typer(project_spec_app, name="spec")
app.add_typer(contact_app, name="contact")
app.add_typer(notification_app, name="notification")
app.add_typer(session_route_app, name="session-route")
app.add_typer(calendar_app, name="calendar")
app.add_typer(event_app, name="event")
app.add_typer(context_app, name="context")
app.add_typer(webhook_app, name="webhook")
app.add_typer(openclaw_app, name="openclaw")
app.add_typer(planning_app, name="planning")
app.add_typer(health_app, name="health")
app.add_typer(learning_app, name="learning")
app.add_typer(phone_app, name="call")
app.add_typer(openai_app, name="openai")
app.add_typer(eval_app, name="eval")


def _service_file_path() -> Path:
    return Path.home() / ".config/systemd/user" / SERVICE_NAME


def _run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, check=True, text=True, capture_output=True)
    except FileNotFoundError:
        typer.echo(f"Command not found: {command[0]}", err=True)
        raise typer.Exit(code=1)
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            typer.echo(exc.stdout, err=False)
        if exc.stderr:
            typer.echo(exc.stderr, err=True)
        raise typer.Exit(code=exc.returncode) from exc


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return _run_command(["systemctl", "--user", *args])


def _service_file_contents(settings: Settings, working_dir: Path) -> str:
    uv_path = shutil.which("uv") or "uv"
    quoted = " ".join(
        shlex.quote(part)
        for part in [
            uv_path,
            "run",
            "cyborg",
            "serve",
            "--host",
            settings.host,
            "--port",
            str(settings.port),
            "--data-dir",
            str(settings.data_dir),
            "--config-dir",
            str(settings.config_dir),
            "--db-path",
            str(settings.db_path),
        ]
    )
    return f"""[Unit]
Description=Cyborg Data Service
After=default.target

[Service]
Type=simple
WorkingDirectory={working_dir}
ExecStart={quoted}
Restart=on-failure
Environment=PYTHONUNBUFFERED=1
Environment=CYBORG_CONFIG_DIR={settings.config_dir}

[Install]
WantedBy=default.target
"""


def _health_status(settings: Settings) -> str:
    try:
        with urlopen(f"http://{settings.host}:{settings.port}/health", timeout=2) as response:
            return response.read().decode("utf-8")
    except URLError as exc:
        return f"unreachable ({exc.reason})"


def _normalize_api_response(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and "data" in payload:
        return payload
    return {"data": payload}


def _handle_http_error(exc: HTTPError) -> None:
    error_body = exc.read().decode()
    try:
        error_data = json.loads(error_body)
        message = error_data.get("detail", error_data.get("error", error_body))
        typer.echo(f"Error: {message}", err=True)
    except json.JSONDecodeError:
        typer.echo(f"Error: {error_body}", err=True)
    raise typer.Exit(code=1) from exc


def _handle_connection_error(exc: URLError) -> None:
    typer.echo(f"Connection error: {exc.reason}", err=True)
    typer.echo("Is the cyborg service running? Try: cyborg start", err=True)
    raise typer.Exit(code=1) from exc


def _api_call(method: str, path: str, data: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    settings = Settings.from_env()
    url = f"http://{settings.host}:{settings.port}{path}"
    headers = {"Content-Type": "application/json"}
    body = json.dumps(data).encode() if data is not None else None
    req = Request(url, data=body, headers=headers, method=method)

    try:
        with urlopen(req, timeout=30) as response:
            response_body = response.read()
            if not response_body:
                return {"data": None}
            return _normalize_api_response(json.loads(response_body.decode()))
    except HTTPError as exc:
        _handle_http_error(exc)
    except URLError as exc:
        _handle_connection_error(exc)


def _text_call(path: str) -> str:
    settings = Settings.from_env()
    url = f"http://{settings.host}:{settings.port}{path}"
    req = Request(url, method="GET")

    try:
        with urlopen(req, timeout=30) as response:
            return response.read().decode()
    except HTTPError as exc:
        _handle_http_error(exc)
    except URLError as exc:
        _handle_connection_error(exc)


def _echo_json(value: Any) -> None:
    typer.echo(json.dumps(value, indent=2))


def _query_string(**params: Any) -> str:
    filtered = {key: value for key, value in params.items() if value is not None}
    if not filtered:
        return ""
    return f"?{urlencode(filtered, doseq=True)}"


def _resolve_inbox_id(inbox_id: Optional[str]) -> str:
    """Resolve inbox_id: explicit value → config default → sole active inbox."""
    if inbox_id:
        return inbox_id
    settings = Settings.from_env()
    if settings.agentmail.default_inbox_id:
        return settings.agentmail.default_inbox_id
    result = _api_call("GET", "/api/v1/email/inboxes?active_only=true")
    inboxes = result.get("data", [])
    if len(inboxes) == 1:
        return str(inboxes[0]["id"])
    if not inboxes:
        raise typer.BadParameter("No active email inboxes found. Register one with `cyborg email-inbox register`.")
    raise typer.BadParameter(
        "Multiple inboxes found. Specify --inbox with one of:\n"
        + "\n".join(f"  {ib['id']}  ({ib.get('email_address', ib.get('display_name', ''))})" for ib in inboxes)
    )


def _parse_json_option(value: str, label: str, expected_type: type[Any]) -> Any:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"{label} must be valid JSON") from exc
    if not isinstance(parsed, expected_type):
        expected_name = expected_type.__name__
        raise typer.BadParameter(f"{label} must decode to a {expected_name}")
    return parsed


def _build_metadata(
    metadata_json: Optional[str],
    channel: Optional[str],
    chat_id: Optional[str],
    session_key: Optional[str],
) -> dict[str, Any] | None:
    explicit = metadata_json is not None
    metadata: dict[str, Any] = {}
    if metadata_json is not None:
        metadata.update(_parse_json_option(metadata_json, "metadata-json", dict))
    if channel is not None:
        metadata["channel"] = channel
    if chat_id is not None:
        metadata["chat_id"] = chat_id
    if session_key is not None:
        metadata["session_key"] = session_key
    if metadata or explicit:
        return metadata
    return None


def _merge_task_target_session(
    metadata: dict[str, Any] | None,
    target_kind: Optional[str],
    target_session_key: Optional[str],
    target_chat_id: Optional[str],
    target_contact_id: Optional[str],
) -> dict[str, Any] | None:
    if (
        metadata is None
        and target_kind is None
        and target_session_key is None
        and target_chat_id is None
        and target_contact_id is None
    ):
        return None

    merged = dict(metadata or {})
    existing_target = merged.get("target_session")
    if existing_target is None:
        target_session: dict[str, Any] = {}
    elif isinstance(existing_target, dict):
        target_session = dict(existing_target)
    else:
        raise typer.BadParameter("metadata-json target_session must be an object")

    if any(value is not None for value in (target_kind, target_session_key, target_chat_id, target_contact_id)):
        target_session["channel"] = "whatsapp"
    if target_kind is not None:
        target_session["kind"] = target_kind
    if target_session_key is not None:
        target_session["session_key"] = target_session_key
    if target_chat_id is not None:
        target_session["chat_id"] = target_chat_id
    if target_contact_id is not None:
        target_session["contact_id"] = target_contact_id
    if target_session:
        merged["target_session"] = target_session
    return merged


def _build_retry_config(
    max_attempts: Optional[int],
    current_attempt: Optional[int],
    on_failure: Optional[str],
    retry_from_step: Optional[int],
) -> dict[str, Any] | None:
    retry_config: dict[str, Any] = {}
    if max_attempts is not None:
        retry_config["max_attempts"] = max_attempts
    if current_attempt is not None:
        retry_config["current_attempt"] = current_attempt
    if on_failure is not None:
        retry_config["on_failure"] = on_failure
    if retry_from_step is not None:
        retry_config["retry_from_step"] = retry_from_step
    return retry_config or None


def _parse_time_expression(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        if value == "now":
            return datetime.now()
        if value.startswith("+") and len(value) > 2:
            amount_text = value[1:-1]
            unit = value[-1]
            if amount_text.isdigit():
                amount = int(amount_text)
                if unit == "m":
                    return datetime.now() + timedelta(minutes=amount)
                if unit == "h":
                    return datetime.now() + timedelta(hours=amount)
                if unit == "d":
                    return datetime.now() + timedelta(days=amount)
        raise typer.BadParameter(f"Cannot parse time value: {value}")


def _time_to_iso(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return _parse_time_expression(value).isoformat()


def _resolve_calendar_id(calendar_id: Optional[str]) -> str:
    if calendar_id:
        return calendar_id
    calendars = _api_call("GET", "/api/v1/calendars")["data"]
    if not calendars:
        typer.echo("No calendars found. Create one first.", err=True)
        raise typer.Exit(code=1)
    default_calendar = next((calendar for calendar in calendars if calendar.get("is_default")), calendars[0])
    return default_calendar["id"]


def _build_task_payload(
    *,
    title: Optional[str] = None,
    requested_by: Optional[str] = None,
    priority: Optional[str] = None,
    description: Optional[str] = None,
    plan: Optional[str] = None,
    status: Optional[str] = None,
    parent_id: Optional[str] = None,
    project_ids: Optional[list[str]] = None,
    recurrence_rule: Optional[str] = None,
    is_recurring: Optional[bool] = None,
    next_run_at: Optional[str] = None,
    retry_max_attempts: Optional[int] = None,
    retry_current_attempt: Optional[int] = None,
    retry_on_failure: Optional[str] = None,
    retry_from_step: Optional[int] = None,
    metadata_json: Optional[str] = None,
    channel: Optional[str] = None,
    chat_id: Optional[str] = None,
    session_key: Optional[str] = None,
    target_kind: Optional[str] = None,
    target_session_key: Optional[str] = None,
    target_chat_id: Optional[str] = None,
    target_contact_id: Optional[str] = None,
    blocked_reason: Optional[str] = None,
    blocked_resume_instructions: Optional[str] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if title is not None:
        payload["title"] = title
    if requested_by is not None:
        payload["requested_by"] = requested_by
    if priority is not None:
        payload["priority"] = priority
    if description is not None:
        payload["description"] = description
    if plan is not None:
        payload["plan"] = plan
    if status is not None:
        payload["status"] = status
    if parent_id is not None:
        payload["parent_id"] = parent_id
    if project_ids:
        payload["project_ids"] = project_ids
    elif project_ids == [] and project_ids is not None:
        payload["project_ids"] = []
    if recurrence_rule is not None:
        payload["recurrence_rule"] = recurrence_rule
    if is_recurring is not None:
        payload["is_recurring"] = is_recurring
    elif recurrence_rule is not None:
        payload["is_recurring"] = True
    if next_run_at is not None:
        payload["next_run_at"] = next_run_at
    retry_config = _build_retry_config(
        retry_max_attempts,
        retry_current_attempt,
        retry_on_failure,
        retry_from_step,
    )
    if retry_config is not None:
        payload["retry_config"] = retry_config
    metadata = _build_metadata(metadata_json, channel, chat_id, session_key)
    metadata = _merge_task_target_session(
        metadata,
        target_kind,
        target_session_key,
        target_chat_id,
        target_contact_id,
    )
    if metadata is not None:
        payload["metadata"] = metadata
    if blocked_reason is not None:
        payload["blocked_reason"] = blocked_reason
    if blocked_resume_instructions is not None:
        payload["blocked_resume_instructions"] = blocked_resume_instructions
    return payload


def _build_project_payload(
    *,
    title: Optional[str] = None,
    aim: Optional[str] = None,
    method: Optional[str] = None,
    description: Optional[str] = None,
    state: Optional[str] = None,
    conclusion: Optional[str] = None,
    plan_json: Optional[str] = None,
    success_criteria_json: Optional[str] = None,
    task_ids: Optional[list[str]] = None,
    metadata_json: Optional[str] = None,
    channel: Optional[str] = None,
    chat_id: Optional[str] = None,
    session_key: Optional[str] = None,
    source_project_ids: Optional[list[str]] = None,
    auto_discover_sources: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if title is not None:
        payload["title"] = title
    if aim is not None:
        payload["aim"] = aim
    if method is not None:
        payload["method"] = method
    if description is not None:
        payload["description"] = description
    if state is not None:
        payload["state"] = state
    if conclusion is not None:
        payload["conclusion"] = conclusion
    if plan_json is not None:
        payload["plan"] = _parse_json_option(plan_json, "plan-json", list)
    if success_criteria_json is not None:
        payload["success_criteria"] = _parse_json_option(success_criteria_json, "success-criteria-json", list)
    if task_ids:
        payload["task_ids"] = task_ids
    elif task_ids == [] and task_ids is not None:
        payload["task_ids"] = []
    metadata = _build_metadata(metadata_json, channel, chat_id, session_key)
    if metadata is not None:
        payload["metadata"] = metadata
    if source_project_ids is not None:
        payload["source_project_ids"] = source_project_ids
    payload["auto_discover_sources"] = auto_discover_sources
    return payload


def _build_contact_payload(
    *,
    name: Optional[str] = None,
    phone_number: Optional[str] = None,
    email: Optional[str] = None,
    whatsapp_groups: Optional[list[str]] = None,
    clear_whatsapp_groups: bool = False,
    metadata_json: Optional[str] = None,
    channel: Optional[str] = None,
    chat_id: Optional[str] = None,
    session_key: Optional[str] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if name is not None:
        payload["name"] = name
    if phone_number is not None:
        payload["phone_number"] = phone_number
    if email is not None:
        payload["email"] = email
    if whatsapp_groups is not None:
        payload["whatsapp_groups"] = whatsapp_groups
    elif clear_whatsapp_groups:
        payload["whatsapp_groups"] = []
    metadata = _build_metadata(metadata_json, channel, chat_id, session_key)
    if metadata is not None:
        payload["metadata"] = metadata
    return payload


def _build_session_route_payload(
    *,
    session_key: Optional[str] = None,
    channel: Optional[str] = None,
    kind: Optional[str] = None,
    chat_id: Optional[str] = None,
    contact_id: Optional[str] = None,
    metadata_json: Optional[str] = None,
    is_active: Optional[bool] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if session_key is not None:
        payload["session_key"] = session_key
    if channel is not None:
        payload["channel"] = channel
    if kind is not None:
        payload["kind"] = kind
    if chat_id is not None:
        payload["chat_id"] = chat_id
    if contact_id is not None:
        payload["contact_id"] = contact_id
    if metadata_json is not None:
        payload["metadata"] = _parse_json_option(metadata_json, "metadata-json", dict)
    if is_active is not None:
        payload["is_active"] = is_active
    return payload


def _print_task_table(tasks: list[dict[str, Any]]) -> None:
    typer.echo(f"{'ID':<36} {'Status':<10} {'Priority':<8} {'Title'}")
    typer.echo("-" * 80)
    for task in tasks:
        typer.echo(f"{task['id']:<36} {task['status']:<10} {task['priority']:<8} {task['title'][:40]}")


def _print_project_table(projects: list[dict[str, Any]]) -> None:
    typer.echo(f"{'ID':<36} {'State':<10} {'Title'}")
    typer.echo("-" * 60)
    for project in projects:
        typer.echo(f"{project['id']:<36} {project['state']:<10} {project['title'][:40]}")


def _print_contact_table(contacts: list[dict[str, Any]]) -> None:
    typer.echo(f"{'ID':<36} {'Phone':<18} {'Name'}")
    typer.echo("-" * 90)
    for contact in contacts:
        typer.echo(f"{contact['id']:<36} {contact['phone_number']:<18} {contact['name'][:32]}")


def _print_notification_table(notifications: list[dict[str, Any]]) -> None:
    typer.echo(f"{'ID':<36} {'Entity':<22} {'Status':<11} {'Delivery':<11} {'Title'}")
    typer.echo("-" * 136)
    for notification in notifications:
        entity = f"{notification['entity_type']}:{notification['notification_type']}"
        typer.echo(
            f"{notification['id']:<36} {entity:<22} {notification['status']:<11} "
            f"{notification.get('delivery_status', 'pending'):<11} {notification['title'][:40]}"
        )


def _print_session_route_table(routes: list[dict[str, Any]]) -> None:
    typer.echo(f"{'ID':<36} {'Channel':<10} {'Kind':<8} {'Active':<8} {'Session Key'}")
    typer.echo("-" * 110)
    for route in routes:
        active = "yes" if route.get("is_active", True) else "no"
        typer.echo(
            f"{route['id']:<36} {route['channel']:<10} {route['kind']:<8} {active:<8} {route['session_key'][:40]}"
        )


def _print_event_table(events: list[dict[str, Any]]) -> None:
    typer.echo(f"{'ID':<36} {'Status':<10} {'Start':<16} {'Title'}")
    typer.echo("-" * 90)
    for event in events:
        start = event["start_time"][:16].replace("T", " ")
        typer.echo(f"{event['id']:<36} {event['status']:<10} {start:<16} {event['title'][:24]}")


def _find_current_plan(task_id: str) -> dict[str, Any]:
    result = _api_call("GET", f"/api/v1/tasks/{task_id}/plans")
    plans_data = result["data"]
    if not plans_data or not plans_data.get("plans"):
        typer.echo("No plans found for this task.", err=True)
        raise typer.Exit(code=1)
    return plans_data["plans"][0]


@app.command()
def install(
    host: Annotated[str, typer.Option(help="Host address for the service")] = DEFAULT_HOST,
    port: Annotated[int, typer.Option(help="TCP port for the service")] = DEFAULT_PORT,
    data_dir: Annotated[Path, typer.Option(help="Directory for the SQLite database")] = Path("~/.local/share/cyborg"),
    config_dir: Annotated[Path, typer.Option(help="Directory for Cyborg config")] = Path("~/.config/cyborg"),
    db_path: Annotated[Path | None, typer.Option(help="Override SQLite database path")] = None,
) -> None:
    """Install and enable the systemd user service."""

    settings = Settings(host=host, port=port, data_dir=data_dir, config_dir=config_dir, db_path=db_path)
    settings.ensure_directories()
    service_path = _service_file_path()
    service_path.parent.mkdir(parents=True, exist_ok=True)
    service_path.write_text(_service_file_contents(settings, Path.cwd().resolve()), encoding="utf-8")
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", SERVICE_NAME)
    typer.echo(f"Installed {SERVICE_NAME} at {service_path}")


@app.command()
def uninstall() -> None:
    """Disable and remove the systemd user service."""

    service_path = _service_file_path()
    if service_path.exists():
        try:
            _systemctl("disable", "--now", SERVICE_NAME)
        except typer.Exit:
            pass
        service_path.unlink()
        _systemctl("daemon-reload")
        typer.echo(f"Removed {service_path}")
    else:
        typer.echo("Service file is not installed")


@app.command()
def start() -> None:
    """Start the systemd user service."""

    _systemctl("start", SERVICE_NAME)
    typer.echo("Service started")


@app.command()
def stop() -> None:
    """Stop the systemd user service."""

    _systemctl("stop", SERVICE_NAME)
    typer.echo("Service stopped")


@app.command()
def restart() -> None:
    """Restart the systemd user service."""

    _systemctl("restart", SERVICE_NAME)
    typer.echo("Service restarted")


@app.command()
def status() -> None:
    """Show systemd state and the HTTP health endpoint."""

    settings = Settings.from_env()
    result = _systemctl("status", "--no-pager", SERVICE_NAME)
    typer.echo(result.stdout)
    typer.echo(f"Health: {_health_status(settings)}")


@app.command()
def logs(
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Follow logs")] = False,
    lines: Annotated[int, typer.Option("--lines", "-n", help="Number of lines to show")] = 200,
) -> None:
    """Print journalctl logs for the service."""

    command = ["journalctl", "--user", "-u", SERVICE_NAME, "--no-pager", "-n", str(lines)]
    if follow:
        command.append("-f")
        subprocess.run(command, check=False)
        return
    result = _run_command(command)
    typer.echo(result.stdout)


@app.command()
def doctor(
    fix: Annotated[bool, typer.Option("--fix", help="Apply fixes to found problems")] = False,
) -> None:
    """Diagnose common project problems and optionally fix them."""

    # Reasoning service can be slow when bootstrapping multiple projects
    settings = Settings.from_env()
    url = f"http://{settings.host}:{settings.port}/api/v1/projects/doctor{_query_string(fix=fix if fix else None)}"
    headers = {"Content-Type": "application/json"}
    req = Request(url, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=120) as response:
            result = _normalize_api_response(json.loads(response.read().decode()))
    except HTTPError as exc:
        _handle_http_error(exc)
    except URLError as exc:
        _handle_connection_error(exc)
    result = result.get("data", result)
    problems = result.get("problems", [])
    fixes = result.get("fixes", [])

    if not problems:
        typer.echo("No problems found.")
        return

    typer.echo(f"Found {len(problems)} problem(s):")
    for p in problems:
        pid = p.get("project_id") or p.get("approval_id") or "?"
        typer.echo(f"  - {p['title']} ({pid[:8]}): {p['problem']}")

    if fixes:
        typer.echo(f"\nApplied {len(fixes)} fix(es):")
        for f in fixes:
            fid = f.get("project_id") or f.get("approval_id") or "?"
            typer.echo(f"  - {f['title']} ({fid[:8]}): {f['action']}")
    elif fix:
        typer.echo("\nNo fixes needed.")


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Host address to bind")] = DEFAULT_HOST,
    port: Annotated[int, typer.Option(help="TCP port to bind")] = DEFAULT_PORT,
    data_dir: Annotated[Path, typer.Option(help="Directory for the SQLite database")] = Path("~/.local/share/cyborg"),
    config_dir: Annotated[Path, typer.Option(help="Directory for config files")] = Path("~/.config/cyborg"),
    db_path: Annotated[Path | None, typer.Option(help="Override SQLite database path")] = None,
    log_level: Annotated[str, typer.Option(help="Uvicorn log level")] = "info",
) -> None:
    """Run the API server directly."""

    previous_config_dir = os.environ.get("CYBORG_CONFIG_DIR")
    os.environ["CYBORG_CONFIG_DIR"] = str(config_dir.expanduser())
    try:
        env_settings = Settings.from_env()
    finally:
        if previous_config_dir is None:
            os.environ.pop("CYBORG_CONFIG_DIR", None)
        else:
            os.environ["CYBORG_CONFIG_DIR"] = previous_config_dir

    settings = Settings(
        host=host,
        port=port,
        data_dir=data_dir,
        config_dir=config_dir,
        db_path=db_path,
        log_level=log_level,
        pool_size=env_settings.pool_size,
        webhooks=env_settings.webhooks,
        openclaw=env_settings.openclaw,
        agentmail=env_settings.agentmail,
        email_polling_enabled=env_settings.email_polling_enabled,
        heartbeat_interval_seconds=env_settings.heartbeat_interval_seconds,
        projects_base_dir=env_settings.projects_base_dir,
        public_url=env_settings.public_url,
        dashboard_secret=env_settings.dashboard_secret,
        voice=env_settings.voice,
        phone=env_settings.phone,
        dispatch_shutdown_timeout_seconds=env_settings.dispatch_shutdown_timeout_seconds,
        dispatch_stuck_timeout_minutes=env_settings.dispatch_stuck_timeout_minutes,
        dispatch_concurrency_limit=env_settings.dispatch_concurrency_limit,
        openai=env_settings.openai,
        harness=env_settings.harness,
        whatsapp_bridge=env_settings.whatsapp_bridge,
    )
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level=settings.log_level)


@task_app.command("list")
def task_list(
    status: Annotated[Optional[str], typer.Option("--status", "-s", help="Filter by status")] = None,
    priority: Annotated[Optional[str], typer.Option("--priority", "-p", help="Filter by priority")] = None,
    parent_id: Annotated[Optional[str], typer.Option("--parent-id", help="Filter by parent task ID")] = None,
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (table, json)")] = "table",
) -> None:
    """List tasks."""

    result = _api_call("GET", f"/api/v1/tasks{_query_string(status=status, parent_id=parent_id)}")
    tasks = result["data"]
    if priority:
        tasks = [task for task in tasks if task["priority"] == priority]

    if format == "json":
        _echo_json(tasks)
        return
    if not tasks:
        typer.echo("No tasks found.")
        return
    _print_task_table(tasks)


@task_app.command("get")
def task_get(
    id: Annotated[str, typer.Argument(help="Task ID")],
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (text, json)")] = "text",
) -> None:
    """Get task details."""

    task = _api_call("GET", f"/api/v1/tasks/{id}")["data"]
    if format == "json":
        _echo_json(task)
        return

    typer.echo(f"ID: {task['id']}")
    typer.echo(f"Title: {task['title']}")
    typer.echo(f"Status: {task['status']}")
    typer.echo(f"Priority: {task['priority']}")
    if task.get("description"):
        typer.echo(f"Description: {task['description']}")
    if task.get("plan"):
        typer.echo(f"Plan: {task['plan']}")
    if task.get("retry_config"):
        typer.echo(f"Retry Config: {json.dumps(task['retry_config'])}")
    if task.get("project_ids"):
        typer.echo(f"Projects: {', '.join(task['project_ids'])}")
    if task.get("metadata"):
        typer.echo(f"Metadata: {json.dumps(task['metadata'])}")
    if task.get("blocked_reason"):
        typer.echo(f"Blocked Reason: {task['blocked_reason']}")
    if task.get("blocked_resume_instructions"):
        typer.echo(f"Resume Instructions: {task['blocked_resume_instructions']}")


@task_app.command("update")
def task_update(
    id: Annotated[str, typer.Argument(help="Task ID")],
    title: Annotated[Optional[str], typer.Option(help="Task title")] = None,
    requested_by: Annotated[Optional[str], typer.Option("--requested-by", "-r", help="Who requested the task")] = None,
    priority: Annotated[Optional[str], typer.Option("--priority", "-p", help="Task priority")] = None,
    description: Annotated[Optional[str], typer.Option("--description", "-d", help="Task description")] = None,
    plan: Annotated[Optional[str], typer.Option(help="Execution plan")] = None,
    status: Annotated[Optional[str], typer.Option("--status", help="Task status")] = None,
    parent_id: Annotated[Optional[str], typer.Option("--parent-id", help="Parent task ID")] = None,
    project_ids: Annotated[Optional[list[str]], typer.Option("--project-id", help="Associated project ID")] = None,
    recurrence_rule: Annotated[Optional[str], typer.Option("--recurrence-rule", help="Cron expression for recurring task")] = None,
    is_recurring: Annotated[Optional[bool], typer.Option("--is-recurring/--not-recurring", help="Explicitly mark the task as recurring or not")] = None,
    next_run_at: Annotated[Optional[str], typer.Option("--next-run-at", help="Next scheduled run time (ISO format)")] = None,
    retry_max_attempts: Annotated[Optional[int], typer.Option("--retry-max-attempts", help="Retry policy max attempts")] = None,
    retry_current_attempt: Annotated[Optional[int], typer.Option("--retry-current-attempt", help="Current retry attempt count")] = None,
    retry_on_failure: Annotated[Optional[str], typer.Option("--retry-on-failure", help="Retry action: retry, retry_from, escalate, abort")] = None,
    retry_from_step: Annotated[Optional[int], typer.Option("--retry-from-step", help="Restart from this step number")] = None,
    metadata_json: Annotated[Optional[str], typer.Option("--metadata-json", help="Task metadata as JSON object")] = None,
    channel: Annotated[Optional[str], typer.Option(help="Source channel for notifications and approvals")] = None,
    chat_id: Annotated[Optional[str], typer.Option(help="Source chat ID for notifications and approvals")] = None,
    session_key: Annotated[Optional[str], typer.Option(help="Source session key for routing")] = None,
    target_kind: Annotated[Optional[str], typer.Option("--target-kind", help="Target session kind: group or dm")] = None,
    target_session_key: Annotated[Optional[str], typer.Option("--target-session-key", help="Target WhatsApp group session key")] = None,
    target_chat_id: Annotated[Optional[str], typer.Option("--target-chat-id", help="Target WhatsApp chat ID")] = None,
    target_contact_id: Annotated[Optional[str], typer.Option("--target-contact-id", help="Target contact ID for WhatsApp DM")] = None,
    blocked_reason: Annotated[Optional[str], typer.Option("--blocked-reason", help="Blocked reason")] = None,
    blocked_resume_instructions: Annotated[Optional[str], typer.Option("--blocked-resume-instructions", help="How to resume the task")] = None,
) -> None:
    """Update a task."""

    payload = _build_task_payload(
        title=title,
        requested_by=requested_by,
        priority=priority,
        description=description,
        plan=plan,
        status=status,
        parent_id=parent_id,
        project_ids=project_ids,
        recurrence_rule=recurrence_rule,
        is_recurring=is_recurring,
        next_run_at=next_run_at,
        retry_max_attempts=retry_max_attempts,
        retry_current_attempt=retry_current_attempt,
        retry_on_failure=retry_on_failure,
        retry_from_step=retry_from_step,
        metadata_json=metadata_json,
        channel=channel,
        chat_id=chat_id,
        session_key=session_key,
        target_kind=target_kind,
        target_session_key=target_session_key,
        target_chat_id=target_chat_id,
        target_contact_id=target_contact_id,
        blocked_reason=blocked_reason,
        blocked_resume_instructions=blocked_resume_instructions,
    )
    task = _api_call("PUT", f"/api/v1/tasks/{id}", payload)["data"]
    typer.echo(f"Updated task: {task['id']}")
    typer.echo(f"Title: {task['title']}")
    typer.echo(f"Status: {task['status']}")


@task_app.command("start")
def task_start(id: Annotated[str, typer.Argument(help="Task ID")]) -> None:
    """Start a task."""

    result = _api_call("POST", f"/api/v1/tasks/{id}/start")
    typer.echo(f"Task started: {result['data']['title']}")


@task_app.command("complete")
def task_complete(
    id: Annotated[str, typer.Argument(help="Task ID")],
    result_summary: Annotated[Optional[str], typer.Option("--result-summary", "-s", help="Summary of task results")] = None,
) -> None:
    """Complete a task."""

    data = {"result_summary": result_summary} if result_summary else None
    result = _api_call("POST", f"/api/v1/tasks/{id}/complete", data)
    typer.echo(f"Task completed: {result['data']['title']}")


@task_app.command("submit")
def task_submit(
    id: Annotated[str, typer.Argument(help="Task ID")],
    result_summary: Annotated[Optional[str], typer.Option("--result-summary", "-s", help="Summary of task results")] = None,
) -> None:
    """Submit a task for review (used by AI agents)."""

    data = {"result_summary": result_summary} if result_summary else None
    result = _api_call("POST", f"/api/v1/tasks/{id}/submit", data)
    task = result["data"]
    typer.echo(f"Task submitted for review: {task['title']} (status: {task['status']})")


@task_app.command("verify-submit")
def task_verify_submit(
    id: Annotated[str, typer.Argument(help="Task ID")],
    otp: Annotated[str, typer.Option("--otp", help="One-time password from the submission review prompt")],
    approve: Annotated[bool, typer.Option("--approve", help="Approve the submission")] = False,
    reject: Annotated[bool, typer.Option("--reject", help="Reject the submission")] = False,
    reason: Annotated[Optional[str], typer.Option("--reason", "-r", help="Reason for rejection")] = None,
) -> None:
    """Verify a task submission with a one-time password (used by AI agents)."""

    if not approve and not reject:
        typer.echo("Error: specify --approve or --reject", err=True)
        raise typer.Exit(1)
    if approve and reject:
        typer.echo("Error: specify --approve or --reject, not both", err=True)
        raise typer.Exit(1)

    data: dict[str, Any] = {"otp": otp, "approved": approve}
    if reject and reason:
        data["reason"] = reason
    result = _api_call("POST", f"/api/v1/tasks/{id}/verify-submit", data)
    task = result["data"]
    if task["status"] == "completed":
        typer.echo(f"Submission approved: {task['title']}")
    elif task["status"] == "active":
        typer.echo(f"Submission rejected, task back to active: {task['title']}")
    else:
        typer.echo(f"Verification result: {task['title']} (status: {task['status']})")


@task_app.command("block")
def task_block(
    id: Annotated[str, typer.Argument(help="Task ID")],
    reason: Annotated[str, typer.Option("--reason", "-r", help="Why the task is blocked")],
    resume_instructions: Annotated[str, typer.Option("--resume-instructions", "-i", help="Full instructions to resume the task")],
    input_schema_json: Annotated[Optional[str], typer.Option("--input-schema-json", help="Structured input schema as JSON (text or multi_choice)") ] = None,
) -> None:
    """Block a task waiting for user input.

    Optionally include --input-schema-json to create a structured dashboard approval
    that the user can respond to from the approvals page.
    """

    payload: dict[str, Any] = {"reason": reason, "resume_instructions": resume_instructions}
    if input_schema_json is not None:
        payload["input_schema"] = _parse_json_option(input_schema_json, "input-schema-json", dict)
    task = _api_call(
        "POST",
        f"/api/v1/tasks/{id}/block",
        payload,
    )["data"]
    typer.echo(f"Task blocked: {task['title']}")
    typer.echo(f"Reason: {task['blocked_reason']}")


@task_app.command("unblock")
def task_unblock(
    id: Annotated[str, typer.Argument(help="Task ID")],
    notes: Annotated[Optional[str], typer.Option("--notes", "-n", help="Notes about why unblocking")] = None,
) -> None:
    """Unblock a task and resume work."""

    data = {"notes": notes} if notes else None
    result = _api_call("POST", f"/api/v1/tasks/{id}/unblock", data)
    typer.echo(f"Task unblocked: {result['data']['title']}")


@task_app.command("retry")
def task_retry(
    id: Annotated[str, typer.Argument(help="Task ID")],
    details_json: Annotated[Optional[str], typer.Option("--details-json", help="Retry details as JSON object")] = None,
) -> None:
    """Retry a failed task."""

    data = {"details": _parse_json_option(details_json, "details-json", dict)} if details_json else {}
    result = _api_call("POST", f"/api/v1/tasks/{id}/retry", data)
    typer.echo(f"Task retried: {result['data']['title']}")


@task_app.command("fail")
def task_fail(
    id: Annotated[str, typer.Argument(help="Task ID")],
    reason: Annotated[Optional[str], typer.Option("--reason", "-r", help="Failure reason")] = None,
    result_text: Annotated[Optional[str], typer.Option("--result", help="Failure result summary")] = None,
    details_json: Annotated[Optional[str], typer.Option("--details-json", help="Failure details as JSON object")] = None,
) -> None:
    """Mark a task as failed."""

    details = _parse_json_option(details_json, "details-json", dict) if details_json else {}
    if reason:
        details["reason"] = reason
    payload: dict[str, Any] = {"details": details}
    if result_text is not None:
        payload["result"] = result_text
    result = _api_call("POST", f"/api/v1/tasks/{id}/fail", payload)
    typer.echo(f"Task failed: {result['data']['title']}")


@task_app.command("steps")
def task_steps(
    task_id: Annotated[str, typer.Argument(help="Task ID")],
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (table, json)")] = "table",
) -> None:
    """List task steps."""

    steps = _api_call("GET", f"/api/v1/tasks/{task_id}/steps")["data"]
    if format == "json":
        _echo_json(steps)
        return
    if not steps:
        typer.echo("No steps found for this task.")
        return
    typer.echo(f"{'Step':<6} {'Status':<10} {'Description'}")
    typer.echo("-" * 80)
    for step in steps:
        typer.echo(f"{step['step_number']:<6} {step['status']:<10} {step['description'][:60]}")


@task_app.command("step-add")
def task_step_add(
    task_id: Annotated[str, typer.Argument(help="Task ID")],
    description: Annotated[str, typer.Option("--description", "-d", help="Step description")],
    step_number: Annotated[int, typer.Option("--step-number", "-n", help="Step number")] = 1,
    status: Annotated[str, typer.Option("--status", help="Step status")] = "pending",
    result_text: Annotated[Optional[str], typer.Option("--result", help="Step result")] = None,
    started_at: Annotated[Optional[str], typer.Option("--started-at", help="Step start time (ISO format)")] = None,
    completed_at: Annotated[Optional[str], typer.Option("--completed-at", help="Step completion time (ISO format)")] = None,
) -> None:
    """Create or update a task step."""

    payload: dict[str, Any] = {
        "step_number": step_number,
        "description": description,
        "status": status,
    }
    if result_text is not None:
        payload["result"] = result_text
    if started_at is not None:
        payload["started_at"] = started_at
    if completed_at is not None:
        payload["completed_at"] = completed_at
    step = _api_call("POST", f"/api/v1/tasks/{task_id}/steps", payload)["data"]
    typer.echo(f"Upserted step {step['step_number']} for task {task_id}")


@task_app.command("history")
def task_history(
    task_id: Annotated[str, typer.Argument(help="Task ID")],
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (table, json)")] = "table",
) -> None:
    """List task history."""

    history = _api_call("GET", f"/api/v1/tasks/{task_id}/history")["data"]
    if format == "json":
        _echo_json(history)
        return
    if not history:
        typer.echo("No history found for this task.")
        return
    typer.echo(f"{'Timestamp':<20} {'Action':<20} {'Details'}")
    typer.echo("-" * 100)
    for item in history:
        timestamp = item["timestamp"][:19].replace("T", " ")
        details = json.dumps(item.get("details", {}))
        typer.echo(f"{timestamp:<20} {item['action']:<20} {details[:56]}")


@task_app.command("delete")
def task_delete(id: Annotated[str, typer.Argument(help="Task ID")]) -> None:
    """Delete (soft delete) a task."""

    _api_call("DELETE", f"/api/v1/tasks/{id}")
    typer.echo(f"Task deleted: {id}")


@task_app.command("file")
def task_file(
    id: Annotated[str, typer.Argument(help="Task ID")],
    project_id: Annotated[str, typer.Option("--project-id", "-p", help="Project ID")],
    filename: Annotated[str, typer.Option("--filename", "-f", help="Filename")],
    purpose: Annotated[
        str,
        typer.Option(
            "--purpose",
            help="File purpose: reasoning, result, analysis, log, artifact, other",
        ),
    ] = "artifact",
    description: Annotated[
        Optional[str], typer.Option("--description", "-d", help="File description")
    ] = None,
) -> None:
    """Register a file created during task execution."""

    payload = {
        "project_id": project_id,
        "file": {
            "filename": filename,
            "purpose": purpose,
            "content_type": "text/plain",
        },
    }
    if description:
        payload["file"]["description"] = description

    result = _api_call("POST", f"/api/v1/tasks/{id}/files", payload)
    f = result["data"]
    typer.echo(f"File registered: {f['filename']} ({f['purpose']}) -> task {id}")


@plan_app.command("submit")
def plan_submit(
    task_id: Annotated[str, typer.Argument(help="Task ID")],
    content: Annotated[str, typer.Option("--content", "-c", help="Plan content/steps")],
) -> None:
    """Submit a plan for task approval."""

    plan = _api_call("POST", f"/api/v1/tasks/{task_id}/plans", {"content": content})["data"]
    typer.echo(f"Plan submitted: {plan['id']}")
    typer.echo(f"Version: {plan['version_number']}")
    typer.echo(f"Status: {plan['status']}")


@plan_app.command("list")
def plan_list(
    task_id: Annotated[str, typer.Argument(help="Task ID")],
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (table, json)")] = "table",
) -> None:
    """List all plans for a task."""

    plans_data = _api_call("GET", f"/api/v1/tasks/{task_id}/plans")["data"]
    if not plans_data or not plans_data.get("plans"):
        typer.echo("No plans found for this task.")
        return

    plans = plans_data["plans"]
    if format == "json":
        _echo_json(plans_data)
        return
    typer.echo(f"{'Version':<8} {'Status':<18} {'Created':<20} {'Content Preview'}")
    typer.echo("-" * 90)
    for plan in plans:
        content_preview = plan["content"][:40] + "..." if len(plan["content"]) > 40 else plan["content"]
        created = plan["created_at"][:16].replace("T", " ")
        typer.echo(f"{plan['version_number']:<8} {plan['status']:<18} {created:<20} {content_preview}")


@plan_app.command("get")
def plan_get(plan_id: Annotated[str, typer.Argument(help="Plan ID")]) -> None:
    """Get a specific plan by ID."""

    _echo_json(_api_call("GET", f"/api/v1/plans/{plan_id}")["data"])


@plan_app.command("approve")
def plan_approve(
    task_id: Annotated[str, typer.Argument(help="Task ID")],
    approver: Annotated[str, typer.Option("--approver", "-a", help="Name of approver")] = "Mike",
) -> None:
    """Approve the current plan for a task."""

    current_plan = _find_current_plan(task_id)
    plan = _api_call("POST", f"/api/v1/plans/{current_plan['id']}/approve", {"approver": approver})["data"]
    typer.echo(f"Plan approved: {plan['id']}")
    typer.echo(f"Approved by: {plan['approved_by']}")


@plan_app.command("approve-id")
def plan_approve_id(
    plan_id: Annotated[str, typer.Argument(help="Plan ID")],
    approver: Annotated[str, typer.Option("--approver", "-a", help="Name of approver")] = "Mike",
) -> None:
    """Approve a specific plan by ID."""

    plan = _api_call("POST", f"/api/v1/plans/{plan_id}/approve", {"approver": approver})["data"]
    typer.echo(f"Plan approved: {plan['id']}")
    typer.echo(f"Approved by: {plan['approved_by']}")


@plan_app.command("reject")
def plan_reject(
    task_id: Annotated[str, typer.Argument(help="Task ID")],
    feedback: Annotated[str, typer.Option("--feedback", "-f", help="Rejection feedback")],
) -> None:
    """Reject the current plan for a task."""

    current_plan = _find_current_plan(task_id)
    plan = _api_call("POST", f"/api/v1/plans/{current_plan['id']}/reject", {"feedback": feedback})["data"]
    typer.echo(f"Plan rejected: {plan['id']}")
    typer.echo(f"Feedback: {plan['feedback']}")


@plan_app.command("reject-id")
def plan_reject_id(
    plan_id: Annotated[str, typer.Argument(help="Plan ID")],
    feedback: Annotated[str, typer.Option("--feedback", "-f", help="Rejection feedback")],
) -> None:
    """Reject a specific plan by ID."""

    plan = _api_call("POST", f"/api/v1/plans/{plan_id}/reject", {"feedback": feedback})["data"]
    typer.echo(f"Plan rejected: {plan['id']}")
    typer.echo(f"Feedback: {plan['feedback']}")


@project_app.command("create")
def project_create(
    title: Annotated[str, typer.Argument(help="Project title")],
    aim: Annotated[Optional[str], typer.Option("--aim", "-a", help="Project aim/objective")] = None,
    method: Annotated[Optional[str], typer.Option("--method", "-m", help="Project method/plan")] = None,
    description: Annotated[Optional[str], typer.Option("--description", "-d", help="Project description")] = None,
    state: Annotated[Optional[str], typer.Option("--state", help="Initial project state")] = None,
    conclusion: Annotated[Optional[str], typer.Option("--conclusion", help="Project conclusion")] = None,
    plan_json: Annotated[Optional[str], typer.Option("--plan-json", help="Execution plan as JSON array")] = None,
    success_criteria_json: Annotated[Optional[str], typer.Option("--success-criteria-json", help="Success criteria as JSON array")] = None,
    task_ids: Annotated[Optional[list[str]], typer.Option("--task-id", help="Link an existing task ID")] = None,
    metadata_json: Annotated[Optional[str], typer.Option("--metadata-json", help="Project metadata as JSON object")] = None,
    channel: Annotated[Optional[str], typer.Option(help="Source channel for approvals and notifications")] = None,
    chat_id: Annotated[Optional[str], typer.Option(help="Source chat ID for approvals and notifications")] = None,
    session_key: Annotated[Optional[str], typer.Option(help="Source session key for routing")] = None,
    source_project_ids: Annotated[Optional[list[str]], typer.Option("--source-project", help="UUID of a source project to derive from")] = None,
    auto_discover_sources: Annotated[bool, typer.Option("--auto-discover/--no-auto-discover", help="Auto-discover matching closed projects as sources")] = True,
) -> None:
    """Create a new project."""

    payload = _build_project_payload(
        title=title,
        aim=aim,
        method=method,
        description=description,
        state=state,
        conclusion=conclusion,
        plan_json=plan_json,
        success_criteria_json=success_criteria_json,
        task_ids=task_ids,
        metadata_json=metadata_json,
        channel=channel,
        chat_id=chat_id,
        session_key=session_key,
        source_project_ids=source_project_ids,
        auto_discover_sources=auto_discover_sources,
    )
    project = _api_call("POST", "/api/v1/projects", payload)["data"]
    typer.echo(f"Created project: {project['id']}")
    typer.echo(f"Title: {project['title']}")
    typer.echo(f"State: {project['state']}")
    if project.get("current_spec_id"):
        typer.echo("Spec v1 submitted automatically. Wait for user approval — Cyborg will run the project once approved.")


@project_app.command("list")
def project_list(
    state: Annotated[Optional[str], typer.Option("--state", "-s", help="Filter by state")] = None,
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (table, json)")] = "table",
) -> None:
    """List projects."""

    projects = _api_call("GET", f"/api/v1/projects{_query_string(state=state)}")["data"]
    if format == "json":
        _echo_json(projects)
        return
    if not projects:
        typer.echo("No projects found.")
        return
    _print_project_table(projects)


@project_app.command("get")
def project_get(
    id: Annotated[str, typer.Argument(help="Project ID")],
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (text, json)")] = "text",
) -> None:
    """Get project details."""

    project = _api_call("GET", f"/api/v1/projects/{id}")["data"]
    if format == "json":
        _echo_json(project)
        return

    typer.echo(f"ID: {project['id']}")
    typer.echo(f"Title: {project['title']}")
    typer.echo(f"State: {project['state']}")
    if project.get("current_spec_id"):
        typer.echo(f"Current Spec: {project['current_spec_id']}")
    if project.get("latest_spec_status"):
        typer.echo(f"Latest Spec Status: {project['latest_spec_status']}")
    if project.get("aim"):
        typer.echo(f"Aim: {project['aim']}")
    if project.get("method"):
        typer.echo(f"Method: {project['method']}")
    if project.get("description"):
        typer.echo(f"Description: {project['description']}")
    if project.get("task_ids"):
        typer.echo(f"Tasks: {len(project['task_ids'])} linked")


@project_app.command("update")
def project_update(
    id: Annotated[str, typer.Argument(help="Project ID")],
    title: Annotated[Optional[str], typer.Option(help="Project title")] = None,
    aim: Annotated[Optional[str], typer.Option("--aim", "-a", help="Project aim/objective")] = None,
    method: Annotated[Optional[str], typer.Option("--method", "-m", help="Project method/plan")] = None,
    description: Annotated[Optional[str], typer.Option("--description", "-d", help="Project description")] = None,
    conclusion: Annotated[Optional[str], typer.Option("--conclusion", help="Project conclusion")] = None,
    plan_json: Annotated[Optional[str], typer.Option("--plan-json", help="Execution plan as JSON array")] = None,
    success_criteria_json: Annotated[Optional[str], typer.Option("--success-criteria-json", help="Success criteria as JSON array")] = None,
    task_ids: Annotated[Optional[list[str]], typer.Option("--task-id", help="Link an existing task ID")] = None,
    metadata_json: Annotated[Optional[str], typer.Option("--metadata-json", help="Project metadata as JSON object")] = None,
    channel: Annotated[Optional[str], typer.Option(help="Source channel for approvals and notifications")] = None,
    chat_id: Annotated[Optional[str], typer.Option(help="Source chat ID for approvals and notifications")] = None,
    session_key: Annotated[Optional[str], typer.Option(help="Source session key for routing")] = None,
) -> None:
    """Update a project."""

    payload = _build_project_payload(
        title=title,
        aim=aim,
        method=method,
        description=description,
        conclusion=conclusion,
        plan_json=plan_json,
        success_criteria_json=success_criteria_json,
        task_ids=task_ids,
        metadata_json=metadata_json,
        channel=channel,
        chat_id=chat_id,
        session_key=session_key,
    )
    project = _api_call("PUT", f"/api/v1/projects/{id}", payload)["data"]
    typer.echo(f"Updated project: {project['id']}")
    typer.echo(f"Title: {project['title']}")
    typer.echo(f"State: {project['state']}")


@project_spec_app.command("submit")
def project_spec_submit(
    project_id: Annotated[str, typer.Argument(help="Project ID")],
    aim: Annotated[str, typer.Option("--aim", "-a", help="Approved project aim")] = ...,
    method: Annotated[str, typer.Option("--method", "-m", help="Approved project method")] = ...,
    success_criteria_json: Annotated[str, typer.Option("--success-criteria-json", help="Success criteria as JSON array")] = ...,
    plan_json: Annotated[Optional[str], typer.Option("--plan-json", help="Execution plan as JSON array")] = None,
) -> None:
    """Submit a project specification for approval."""

    payload: dict[str, Any] = {
        "aim": aim,
        "method": method,
        "success_criteria": _parse_json_option(success_criteria_json, "success-criteria-json", list),
    }
    if plan_json is not None:
        payload["plan"] = _parse_json_option(plan_json, "plan-json", list)
    spec = _api_call("POST", f"/api/v1/projects/{project_id}/specs", payload)["data"]
    typer.echo(f"Submitted project spec: {spec['id']}")
    typer.echo(f"Version: {spec['version_number']}")
    typer.echo(f"Status: {spec['status']}")


@project_spec_app.command("list")
def project_spec_list(
    project_id: Annotated[str, typer.Argument(help="Project ID")],
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (table, json)")] = "table",
) -> None:
    """List project specs."""

    specs_data = _api_call("GET", f"/api/v1/projects/{project_id}/specs")["data"]
    specs = specs_data.get("specs") if specs_data else None
    if format == "json":
        _echo_json(specs_data)
        return
    if not specs:
        typer.echo("No specs found for this project.")
        return
    typer.echo(f"{'Version':<8} {'Status':<18} {'Current':<8} {'Created':<20} {'Aim'}")
    typer.echo("-" * 110)
    for spec in specs:
        created = spec["created_at"][:16].replace("T", " ")
        current = "yes" if spec.get("is_current") else "no"
        typer.echo(f"{spec['version_number']:<8} {spec['status']:<18} {current:<8} {created:<20} {spec['aim'][:32]}")


@project_spec_app.command("get")
def project_spec_get(spec_id: Annotated[str, typer.Argument(help="Project spec ID")]) -> None:
    """Get a specific project spec."""

    _echo_json(_api_call("GET", f"/api/v1/project-specs/{spec_id}")["data"])


@project_app.command("pause")
def project_pause(id: Annotated[str, typer.Argument(help="Project ID")]) -> None:
    """Pause a project."""

    result = _api_call("POST", f"/api/v1/projects/{id}/pause")
    typer.echo(f"Project paused: {result['data']['title']}")


@project_app.command("resume")
def project_resume(id: Annotated[str, typer.Argument(help="Project ID")]) -> None:
    """Resume a paused project."""

    result = _api_call("POST", f"/api/v1/projects/{id}/resume")
    typer.echo(f"Project resumed: {result['data']['title']}")


@project_app.command("mute")
def project_mute(id: Annotated[str, typer.Argument(help="Project ID")]) -> None:
    """Mute project notifications."""

    result = _api_call("POST", f"/api/v1/projects/{id}/mute")
    typer.echo(f"Project muted: {result['data']['title']}")


@project_app.command("unmute")
def project_unmute(id: Annotated[str, typer.Argument(help="Project ID")]) -> None:
    """Unmute project notifications."""

    result = _api_call("POST", f"/api/v1/projects/{id}/unmute")
    typer.echo(f"Project unmuted: {result['data']['title']}")


@project_app.command("close")
def project_close(
    id: Annotated[str, typer.Argument(help="Project ID")],
    conclusion: Annotated[Optional[str], typer.Option("--conclusion", "-c", help="Project conclusion/summary")] = None,
) -> None:
    """Close a project."""

    payload = {"conclusion": conclusion} if conclusion is not None else {}
    result = _api_call("POST", f"/api/v1/projects/{id}/close", payload)
    typer.echo(f"Project closed: {result['data']['title']}")


@project_app.command("tasks")
def project_tasks(
    project_id: Annotated[str, typer.Argument(help="Project ID")],
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (table, json)")] = "table",
) -> None:
    """List tasks for a project."""

    tasks = _api_call("GET", f"/api/v1/projects/{project_id}/tasks")["data"]
    if format == "json":
        _echo_json(tasks)
        return
    if not tasks:
        typer.echo("No tasks found for this project.")
        return
    _print_task_table(tasks)


@project_app.command("journal")
def project_journal(
    project_id: Annotated[str, typer.Argument(help="Project ID")],
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (table, json)")] = "table",
) -> None:
    """List journal entries for a project."""

    journal = _api_call("GET", f"/api/v1/projects/{project_id}/journal")["data"]
    if format == "json":
        _echo_json(journal)
        return
    if not journal:
        typer.echo("No journal entries found for this project.")
        return
    typer.echo(f"{'Created':<20} {'Type':<12} {'Content'}")
    typer.echo("-" * 100)
    for entry in journal:
        created = entry["created_at"][:19].replace("T", " ")
        typer.echo(f"{created:<20} {entry['entry_type']:<12} {entry['content'][:64]}")


@project_app.command("journal-add")
def project_journal_add(
    project_id: Annotated[str, typer.Argument(help="Project ID")],
    content: Annotated[str, typer.Option("--content", "-c", help="Journal entry content")],
    entry_type: Annotated[str, typer.Option("--type", "-t", help="Entry type")] = "note",
    metadata_json: Annotated[Optional[str], typer.Option("--metadata-json", help="Journal metadata as JSON object")] = None,
) -> None:
    """Add a journal entry to a project."""

    payload: dict[str, Any] = {"entry_type": entry_type, "content": content}
    if metadata_json is not None:
        payload["metadata"] = _parse_json_option(metadata_json, "metadata-json", dict)
    entry = _api_call("POST", f"/api/v1/projects/{project_id}/journal", payload)["data"]
    typer.echo(f"Added journal entry: {entry['id']}")
    typer.echo(f"Type: {entry['entry_type']}")


@project_app.command("evaluate")
def project_evaluate(project_id: Annotated[str, typer.Argument(help="Project ID")]) -> None:
    """Evaluate whether a project meets its success criteria."""

    project = _api_call("POST", f"/api/v1/projects/{project_id}/evaluate")["data"]
    if project is None:
        typer.echo("Project does not yet meet success criteria.")
        return
    typer.echo(f"Project completed: {project['title']}")
    typer.echo(f"State: {project['state']}")


@project_app.command("decide-next")
def project_decide_next(
    project_id: Annotated[str, typer.Argument(help="Project ID")],
    otp: Annotated[str, typer.Option("--otp", help="One-time password from the next-action prompt")],
    action: Annotated[str, typer.Option("--action", help="Action: create_task, close_project, or block_project")],
    reasoning: Annotated[str, typer.Option("--reasoning", "-r", help="Why this action was chosen")] = "",
    task_title: Annotated[Optional[str], typer.Option("--task-title", help="Title for create_task")] = None,
    task_description: Annotated[Optional[str], typer.Option("--task-description", help="Description for create_task")] = None,
    task_plan: Annotated[Optional[str], typer.Option("--task-plan", help="Plan for create_task")] = None,
    task_priority: Annotated[Optional[str], typer.Option("--task-priority", help="Priority: high, medium, low")] = None,
    block_reason: Annotated[Optional[str], typer.Option("--block-reason", help="Why blocked (for block_project)")] = None,
    resume_instructions: Annotated[Optional[str], typer.Option("--resume-instructions", help="How to unblock (for block_project)")] = None,
) -> None:
    """Submit a next-action decision for a project (used by AI agents)."""

    valid_actions = ("create_task", "close_project", "block_project")
    if action not in valid_actions:
        typer.echo(f"Error: action must be one of {', '.join(valid_actions)}", err=True)
        raise typer.Exit(1)

    data: dict[str, Any] = {
        "otp": otp,
        "action": action,
        "reasoning": reasoning,
    }
    if action == "create_task":
        if not task_title:
            typer.echo("Error: --task-title is required for create_task", err=True)
            raise typer.Exit(1)
        data["task_title"] = task_title
        if task_description:
            data["task_description"] = task_description
        if task_plan:
            data["task_plan"] = task_plan
        if task_priority:
            data["task_priority"] = task_priority
    elif action == "block_project":
        if block_reason:
            data["block_reason"] = block_reason
        if resume_instructions:
            data["resume_instructions"] = resume_instructions

    result = _api_call("POST", f"/api/v1/projects/{project_id}/decide-next", data)
    project = result["data"]
    typer.echo(f"Decision applied: {action} for {project['title']} (state: {project['state']})")


@project_app.command("list-sources")
def project_list_sources(
    project_id: Annotated[str, typer.Argument(help="Project ID")],
) -> None:
    """List source projects for a derived project."""
    result = _api_call("GET", f"/api/v1/projects/{project_id}/sources")
    sources = result["data"]
    if not sources:
        typer.echo("No source projects linked.")
        return
    for s in sources:
        auto = " (auto)" if s.get("auto_discovered") else ""
        score = f" [{s['relevance_score']:.2f}]" if s.get("relevance_score") else ""
        typer.echo(f"  {s['source_project_id']}  {s['source_project_title']}{auto}{score}")
        if s.get("relevance_reason"):
            typer.echo(f"    Reason: {s['relevance_reason']}")


@project_app.command("add-source")
def project_add_source(
    project_id: Annotated[str, typer.Argument(help="Derived project ID")],
    source_ids: Annotated[list[str], typer.Argument(help="Source project UUID(s)")],
) -> None:
    """Link one or more source projects to a derived project."""
    payload = {"source_project_ids": source_ids}
    result = _api_call("POST", f"/api/v1/projects/{project_id}/sources", payload)
    linked = result["data"]
    typer.echo(f"Linked {len(linked)} source project(s).")


@project_app.command("remove-source")
def project_remove_source(
    project_id: Annotated[str, typer.Argument(help="Derived project ID")],
    source_id: Annotated[str, typer.Argument(help="Source project UUID to unlink")],
) -> None:
    """Remove a source project link."""
    _api_call("DELETE", f"/api/v1/projects/{project_id}/sources/{source_id}")
    typer.echo("Source removed.")


@project_app.command("scan-sources")
def project_scan_sources(
    project_id: Annotated[str, typer.Argument(help="Project ID")],
) -> None:
    """Rescan all linked source projects for outputs."""
    result = _api_call("POST", f"/api/v1/projects/{project_id}/sources/scan")
    outputs = result["data"]
    if not outputs:
        typer.echo("No outputs found.")
        return
    for o in outputs:
        typer.echo(f"  [{o['output_type']}] {o['path']}")
        if o.get("description"):
            typer.echo(f"    {o['description']}")

# ============================================================================
# Planning Commands (AI-powered project planning)
# ============================================================================


@planning_app.command("generate")
def planning_generate(
    aim: Annotated[str, typer.Option("--aim", "-a", help="What the project aims to accomplish")],
    method: Annotated[str, typer.Option("--method", "-m", help="How the project will be executed")] = "",
    success_criteria: Annotated[list[str], typer.Option("--success-criteria", "-s", "-c", help="Success criteria")] = [],
    reference_project_id: Annotated[str, typer.Option("--reference-project", "-r", help="Optional project ID to reference")] = "",
) -> None:
    """Generate a project plan using AI reasoning via OpenClaw.

    Example:
      cyborg planning generate --aim "Launch a customer feedback sprint" --method "Interview customers, cluster themes, summarize top actions"
    """

    payload: dict[str, Any] = {
        "aim": aim,
        "method": method or None,
        "success_criteria": success_criteria,
        "reference_project_id": reference_project_id or None,
    }

    try:
        response = _api_call("POST", "/api/v1/planning/generate-plan", payload)
    except (HTTPError, URLError) as e:
        typer.echo(f"Error generating plan: {e}", err=True)
        raise typer.Exit(1)

    result = response.get("data", response)
    steps = result.get("steps", [])

    typer.echo(f"Generated {len(steps)} step plan:")
    typer.echo()

    for step in steps:
        typer.echo(f"  {step['order'] + 1}. {step['title']}")
        if step.get("description"):
            typer.echo(f"     {step['description']}")
        if step.get("criteria"):
            typer.echo(f"     ✓ {step['criteria']}")
        typer.echo()

    reasoning = result.get("reasoning", "")
    if reasoning:
        typer.echo(f"Reasoning: {reasoning}")


@planning_app.command("refine")
def planning_refine(
    project_id: Annotated[str, typer.Option("--project", "-p", help="Project ID")],
    trigger_task_id: Annotated[str, typer.Option("--task", "-t", help="Task ID that triggered refinement")] = "",
    trigger_reason: Annotated[str, typer.Option("--reason", "-r", help="Reason for refinement (task_completion, failure, manual)")] = "manual",
) -> None:
    """Trigger strategy refinement analysis for a project.

    Uses OpenClaw reasoning to analyze project state and suggest strategic adjustments.

    Example:
      cyborg planning refine --project abc-123 --task def-456
    """

    if not project_id:
        typer.echo("Error: --project is required", err=True)
        raise typer.Exit(1)

    payload: dict[str, Any] = {
        "trigger_task_id": trigger_task_id or None,
        "trigger_reason": trigger_reason,
        "force_refresh": False,
    }

    try:
        response = _api_call("POST", f"/api/v1/planning/projects/{project_id}/refine-strategy", payload)
    except (HTTPError, URLError) as e:
        typer.echo(f"Error refining strategy: {e}", err=True)
        raise typer.Exit(1)

    result = response.get("data", response)

    should_refine = result.get("should_refine", False)
    reasoning = result.get("reasoning", "")
    suggested_changes = result.get("suggested_changes", [])
    risks = result.get("risks_identified", [])

    typer.echo(f"Strategy Refinement for project {project_id}")
    typer.echo()

    if should_refine:
        typer.echo("  ⚠️  Refinement RECOMMENDED")
    else:
        typer.echo("  ✓  No refinement needed")

    typer.echo()
    typer.echo(f"Reasoning: {reasoning}")
    typer.echo()

    if suggested_changes:
        typer.echo("Suggested Changes:")
        for change in suggested_changes:
            typer.echo(f"  - {change}")
        typer.echo()

    if risks:
        typer.echo("Risks Identified:")
        for risk in risks:
            typer.echo(f"  - {risk}")
        typer.echo()

    applied_at = result.get("applied_at")
    if applied_at:
        typer.echo(f"Applied at: {applied_at}")


@contact_app.command("create")
def contact_create(
    name: Annotated[str, typer.Argument(help="Contact name")],
    phone_number: Annotated[str, typer.Option("--phone-number", "--phone", "-p", help="Contact phone number")] = ...,
    email: Annotated[Optional[str], typer.Option("--email", "-e", help="Contact email")] = None,
    whatsapp_groups: Annotated[Optional[list[str]], typer.Option("--whatsapp-group", help="WhatsApp group identifier")] = None,
    metadata_json: Annotated[Optional[str], typer.Option("--metadata-json", help="Contact metadata as JSON object")] = None,
    channel: Annotated[Optional[str], typer.Option(help="Messaging channel for routing")] = None,
    chat_id: Annotated[Optional[str], typer.Option(help="Chat or room identifier for routing")] = None,
    session_key: Annotated[Optional[str], typer.Option(help="OpenClaw session key for routing")] = None,
) -> None:
    """Create a contact."""

    payload = _build_contact_payload(
        name=name,
        phone_number=phone_number,
        email=email,
        whatsapp_groups=whatsapp_groups,
        metadata_json=metadata_json,
        channel=channel,
        chat_id=chat_id,
        session_key=session_key,
    )
    contact = _api_call("POST", "/api/v1/contacts", payload)["data"]
    typer.echo(f"Created contact: {contact['id']}")
    typer.echo(f"Name: {contact['name']}")
    typer.echo(f"Phone: {contact['phone_number']}")


@contact_app.command("list")
def contact_list(
    search: Annotated[Optional[str], typer.Option("--search", "-s", help="Search by name, phone, or email")] = None,
    skip: Annotated[int, typer.Option("--skip", help="Pagination offset")] = 0,
    limit: Annotated[int, typer.Option("--limit", help="Page size")] = 100,
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (table, json)")] = "table",
) -> None:
    """List contacts."""

    contacts = _api_call("GET", f"/api/v1/contacts{_query_string(skip=skip, limit=limit, search=search)}")["data"]
    if format == "json":
        _echo_json(contacts)
        return
    if not contacts:
        typer.echo("No contacts found.")
        return
    _print_contact_table(contacts)


@contact_app.command("get")
def contact_get(
    contact_id: Annotated[str, typer.Argument(help="Contact ID")],
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (text, json)")] = "text",
) -> None:
    """Get contact details."""

    contact = _api_call("GET", f"/api/v1/contacts/{contact_id}")["data"]
    if format == "json":
        _echo_json(contact)
        return
    typer.echo(f"ID: {contact['id']}")
    typer.echo(f"Name: {contact['name']}")
    typer.echo(f"Phone: {contact['phone_number']}")
    if contact.get("email"):
        typer.echo(f"Email: {contact['email']}")
    if contact.get("whatsapp_groups"):
        typer.echo(f"WhatsApp Groups: {', '.join(contact['whatsapp_groups'])}")
    if contact.get("metadata"):
        typer.echo(f"Metadata: {json.dumps(contact['metadata'])}")


@contact_app.command("update")
def contact_update(
    contact_id: Annotated[str, typer.Argument(help="Contact ID")],
    name: Annotated[Optional[str], typer.Option(help="Contact name")] = None,
    phone_number: Annotated[Optional[str], typer.Option("--phone-number", "--phone", "-p", help="Contact phone number")] = None,
    email: Annotated[Optional[str], typer.Option("--email", "-e", help="Contact email")] = None,
    whatsapp_groups: Annotated[Optional[list[str]], typer.Option("--whatsapp-group", help="WhatsApp group identifier")] = None,
    clear_whatsapp_groups: Annotated[bool, typer.Option("--clear-whatsapp-groups", help="Remove all WhatsApp group memberships")] = False,
    metadata_json: Annotated[Optional[str], typer.Option("--metadata-json", help="Contact metadata as JSON object")] = None,
    channel: Annotated[Optional[str], typer.Option(help="Messaging channel for routing")] = None,
    chat_id: Annotated[Optional[str], typer.Option(help="Chat or room identifier for routing")] = None,
    session_key: Annotated[Optional[str], typer.Option(help="OpenClaw session key for routing")] = None,
) -> None:
    """Update a contact."""

    payload = _build_contact_payload(
        name=name,
        phone_number=phone_number,
        email=email,
        whatsapp_groups=whatsapp_groups,
        clear_whatsapp_groups=clear_whatsapp_groups,
        metadata_json=metadata_json,
        channel=channel,
        chat_id=chat_id,
        session_key=session_key,
    )
    contact = _api_call("PUT", f"/api/v1/contacts/{contact_id}", payload)["data"]
    typer.echo(f"Updated contact: {contact['id']}")
    typer.echo(f"Name: {contact['name']}")
    typer.echo(f"Phone: {contact['phone_number']}")


@contact_app.command("delete")
def contact_delete(contact_id: Annotated[str, typer.Argument(help="Contact ID")]) -> None:
    """Delete a contact."""

    _api_call("DELETE", f"/api/v1/contacts/{contact_id}")
    typer.echo(f"Contact deleted: {contact_id}")


@contact_app.command("by-phone")
def contact_by_phone(
    phone_number: Annotated[str, typer.Argument(help="Contact phone number")],
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (text, json)")] = "text",
) -> None:
    """Find a contact by phone number."""

    contact = _api_call("GET", f"/api/v1/contacts/by-phone/{quote(phone_number, safe='')}")["data"]
    if format == "json":
        _echo_json(contact)
        return
    typer.echo(f"ID: {contact['id']}")
    typer.echo(f"Name: {contact['name']}")
    typer.echo(f"Phone: {contact['phone_number']}")


@contact_app.command("by-email")
def contact_by_email(
    email: Annotated[str, typer.Argument(help="Contact email address")],
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (text, json)")] = "text",
) -> None:
    """Find a contact by email."""

    contact = _api_call("GET", f"/api/v1/contacts/by-email/{quote(email, safe='')}")["data"]
    if format == "json":
        _echo_json(contact)
        return
    typer.echo(f"ID: {contact['id']}")
    typer.echo(f"Name: {contact['name']}")
    typer.echo(f"Phone: {contact['phone_number']}")


@contact_app.command("by-whatsapp-group")
def contact_by_whatsapp_group(
    group_id: Annotated[str, typer.Argument(help="WhatsApp group identifier")],
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (table, json)")] = "table",
) -> None:
    """List contacts in a WhatsApp group."""

    contacts = _api_call("GET", f"/api/v1/contacts/by-whatsapp-group/{quote(group_id, safe='')}")["data"]
    if format == "json":
        _echo_json(contacts)
        return
    if not contacts:
        typer.echo("No contacts found.")
        return
    _print_contact_table(contacts)


@contact_app.command("set-default")
def contact_set_default(
    contact_id: Annotated[str, typer.Argument(help="Contact ID to set as default")],
) -> None:
    """Set a contact as the default for notifications."""

    contact = _api_call("PUT", f"/api/v1/contacts/{contact_id}/set-default", {})["data"]
    typer.echo(f"Default contact set: {contact['name']}")
    typer.echo(f"ID: {contact['id']}")
    typer.echo(f"Phone: {contact['phone_number']}")


@contact_app.command("get-default")
def contact_get_default(
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (text, json)")] = "text",
) -> None:
    """Get the current default contact for notifications."""

    try:
        contact = _api_call("GET", "/api/v1/contacts/default")["data"]
    except _HTTPError as e:
        if e.response.status_code == 404:
            typer.echo("No default contact configured.")
            return
        raise

    if format == "json":
        _echo_json(contact)
        return
    typer.echo(f"ID: {contact['id']}")
    typer.echo(f"Name: {contact['name']}")
    typer.echo(f"Phone: {contact['phone_number']}")


@contact_app.command("clear-default")
def contact_clear_default() -> None:
    """Clear the default contact."""

    _api_call("DELETE", "/api/v1/contacts/default")
    typer.echo("Default contact cleared.")


@session_route_app.command("create")
def session_route_create(
    session_key: Annotated[str, typer.Argument(help="Logical session key")],
    kind: Annotated[str, typer.Option("--kind", help="Route kind: group or dm")] = ...,
    chat_id: Annotated[Optional[str], typer.Option("--chat-id", help="Concrete chat or group ID")] = None,
    contact_id: Annotated[Optional[str], typer.Option("--contact-id", help="Contact ID for DM routing")] = None,
    channel: Annotated[str, typer.Option("--channel", help="Messaging channel")] = "whatsapp",
    metadata_json: Annotated[Optional[str], typer.Option("--metadata-json", help="Session route metadata as JSON object")] = None,
) -> None:
    """Create a session routing registry entry."""

    payload = _build_session_route_payload(
        session_key=session_key,
        channel=channel,
        kind=kind,
        chat_id=chat_id,
        contact_id=contact_id,
        metadata_json=metadata_json,
    )
    route = _api_call("POST", "/api/v1/session-routes", payload)["data"]
    typer.echo(f"Created session route: {route['id']}")
    typer.echo(f"Session key: {route['session_key']}")
    typer.echo(f"Kind: {route['kind']}")


@session_route_app.command("list")
def session_route_list(
    channel: Annotated[Optional[str], typer.Option("--channel", help="Filter by channel")] = None,
    all_routes: Annotated[bool, typer.Option("--all", help="Include inactive routes")] = False,
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (table, json)")] = "table",
) -> None:
    """List session routes."""

    routes = _api_call(
        "GET",
        f"/api/v1/session-routes{_query_string(channel=channel, active_only=False if all_routes else True)}",
    )["data"]
    if format == "json":
        _echo_json(routes)
        return
    if not routes:
        typer.echo("No session routes found.")
        return
    _print_session_route_table(routes)


@session_route_app.command("get")
def session_route_get(
    route_id: Annotated[str, typer.Argument(help="Session route ID")],
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (text, json)")] = "text",
) -> None:
    """Get session route details."""

    route = _api_call("GET", f"/api/v1/session-routes/{route_id}")["data"]
    if format == "json":
        _echo_json(route)
        return
    typer.echo(f"ID: {route['id']}")
    typer.echo(f"Session key: {route['session_key']}")
    typer.echo(f"Channel: {route['channel']}")
    typer.echo(f"Kind: {route['kind']}")
    typer.echo(f"Active: {'yes' if route.get('is_active', True) else 'no'}")
    if route.get("chat_id"):
        typer.echo(f"Chat ID: {route['chat_id']}")
    if route.get("contact_id"):
        typer.echo(f"Contact ID: {route['contact_id']}")
    if route.get("metadata"):
        typer.echo(f"Metadata: {json.dumps(route['metadata'])}")


@session_route_app.command("update")
def session_route_update(
    route_id: Annotated[str, typer.Argument(help="Session route ID")],
    chat_id: Annotated[Optional[str], typer.Option("--chat-id", help="Concrete chat or group ID")] = None,
    contact_id: Annotated[Optional[str], typer.Option("--contact-id", help="Contact ID for DM routing")] = None,
    metadata_json: Annotated[Optional[str], typer.Option("--metadata-json", help="Replacement metadata as JSON object")] = None,
    activate: Annotated[bool, typer.Option("--activate", help="Mark the route active")] = False,
    deactivate: Annotated[bool, typer.Option("--deactivate", help="Mark the route inactive")] = False,
) -> None:
    """Update a session route."""

    if activate and deactivate:
        raise typer.BadParameter("Choose only one of --activate or --deactivate")
    is_active = True if activate else False if deactivate else None
    payload = _build_session_route_payload(
        chat_id=chat_id,
        contact_id=contact_id,
        metadata_json=metadata_json,
        is_active=is_active,
    )
    route = _api_call("PUT", f"/api/v1/session-routes/{route_id}", payload)["data"]
    typer.echo(f"Updated session route: {route['id']}")
    typer.echo(f"Active: {'yes' if route.get('is_active', True) else 'no'}")


@session_route_app.command("delete")
def session_route_delete(route_id: Annotated[str, typer.Argument(help="Session route ID")]) -> None:
    """Delete a session route."""

    _api_call("DELETE", f"/api/v1/session-routes/{route_id}")
    typer.echo(f"Session route deleted: {route_id}")


@notification_app.command("list")
def notification_list(
    status: Annotated[Optional[str], typer.Option("--status", "-s", help="Filter by status")] = "pending",
    entity_type: Annotated[Optional[str], typer.Option("--entity-type", "-e", help="Filter by entity type")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Maximum notifications to return")] = 100,
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (table, json)")] = "table",
) -> None:
    """List notifications."""

    notifications = _api_call(
        "GET",
        f"/api/v1/notifications{_query_string(status=status, entity_type=entity_type, limit=limit)}",
    )["data"]
    if format == "json":
        _echo_json(notifications)
        return
    if not notifications:
        typer.echo("No notifications found.")
        return
    _print_notification_table(notifications)


@notification_app.command("get")
def notification_get(
    notification_id: Annotated[str, typer.Argument(help="Notification ID")],
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (text, json)")] = "text",
) -> None:
    """Get notification details."""

    notification = _api_call("GET", f"/api/v1/notifications/{notification_id}")["data"]
    if format == "json":
        _echo_json(notification)
        return
    typer.echo(f"ID: {notification['id']}")
    typer.echo(f"Entity: {notification['entity_type']} / {notification['entity_id']}")
    typer.echo(f"Type: {notification['notification_type']}")
    typer.echo(f"Status: {notification['status']}")
    typer.echo(f"Delivery: {notification.get('delivery_status', 'pending')}")
    typer.echo(f"Title: {notification['title']}")
    typer.echo(f"Message: {notification['message']}")
    if notification.get("sequence_number") is not None:
        typer.echo(f"Sequence: {notification['sequence_number']}")
    if notification.get("last_delivery_error"):
        typer.echo(f"Last Delivery Error: {notification['last_delivery_error']}")
    if notification.get("metadata"):
        typer.echo(f"Metadata: {json.dumps(notification['metadata'])}")


@notification_app.command("ack")
def notification_ack(
    notification_id: Annotated[str, typer.Argument(help="Notification ID")],
    acknowledged_by: Annotated[Optional[str], typer.Option("--acknowledged-by", help="Client or actor acknowledging the notification")] = None,
) -> None:
    """Acknowledge a notification."""

    payload = {"acknowledged_by": acknowledged_by} if acknowledged_by is not None else {}
    notification = _api_call("POST", f"/api/v1/notifications/{notification_id}/acknowledge", payload)["data"]
    typer.echo(f"Acknowledged notification: {notification['id']}")
    typer.echo(f"Status: {notification['status']}")


@notification_app.command("process-due")
def notification_process_due() -> None:
    """Sync and dispatch due notifications immediately."""

    result = _api_call("POST", "/api/v1/notifications/process-due", {})
    processed = result["data"]["processed"]
    typer.echo(f"Processed notifications: {processed}")


@calendar_app.command("create")
def calendar_create(
    name: Annotated[str, typer.Argument(help="Calendar name")],
    description: Annotated[Optional[str], typer.Option("--description", "-d", help="Calendar description")] = None,
    color: Annotated[Optional[str], typer.Option("--color", "-c", help="Calendar color (#RRGGBB)")] = None,
    is_default: Annotated[bool, typer.Option("--default", help="Set as default calendar")] = False,
    metadata_json: Annotated[Optional[str], typer.Option("--metadata-json", help="Calendar metadata as JSON object")] = None,
    channel: Annotated[Optional[str], typer.Option(help="Messaging channel for routing")] = None,
    chat_id: Annotated[Optional[str], typer.Option(help="Chat or room identifier for routing")] = None,
    session_key: Annotated[Optional[str], typer.Option(help="OpenClaw session key for reminder routing")] = None,
) -> None:
    """Create a calendar."""

    payload: dict[str, Any] = {"name": name, "is_default": is_default}
    if description is not None:
        payload["description"] = description
    if color is not None:
        payload["color"] = color
    metadata = _build_metadata(metadata_json, channel, chat_id, session_key)
    if metadata is not None:
        payload["metadata"] = metadata
    calendar = _api_call("POST", "/api/v1/calendars", payload)["data"]
    typer.echo(f"Created calendar: {calendar['id']}")
    typer.echo(f"Name: {calendar['name']}")


@calendar_app.command("list")
def calendar_list(
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (table, json)")] = "table",
) -> None:
    """List calendars."""

    calendars = _api_call("GET", "/api/v1/calendars")["data"]
    if format == "json":
        _echo_json(calendars)
        return
    if not calendars:
        typer.echo("No calendars found.")
        return
    typer.echo(f"{'ID':<36} {'Default':<8} {'Name'}")
    typer.echo("-" * 70)
    for calendar in calendars:
        default_flag = "yes" if calendar["is_default"] else "no"
        typer.echo(f"{calendar['id']:<36} {default_flag:<8} {calendar['name']}")


@calendar_app.command("get")
def calendar_get(calendar_id: Annotated[str, typer.Argument(help="Calendar ID")]) -> None:
    """Get calendar details."""

    _echo_json(_api_call("GET", f"/api/v1/calendars/{calendar_id}")["data"])


@calendar_app.command("update")
def calendar_update(
    calendar_id: Annotated[str, typer.Argument(help="Calendar ID")],
    name: Annotated[Optional[str], typer.Option(help="Calendar name")] = None,
    description: Annotated[Optional[str], typer.Option("--description", "-d", help="Calendar description")] = None,
    color: Annotated[Optional[str], typer.Option("--color", "-c", help="Calendar color (#RRGGBB)")] = None,
    is_default: Annotated[Optional[bool], typer.Option("--default/--not-default", help="Set or unset the default calendar")] = None,
    metadata_json: Annotated[Optional[str], typer.Option("--metadata-json", help="Calendar metadata as JSON object")] = None,
    channel: Annotated[Optional[str], typer.Option(help="Messaging channel for routing")] = None,
    chat_id: Annotated[Optional[str], typer.Option(help="Chat or room identifier for routing")] = None,
    session_key: Annotated[Optional[str], typer.Option(help="OpenClaw session key for reminder routing")] = None,
) -> None:
    """Update a calendar."""

    payload: dict[str, Any] = {}
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description
    if color is not None:
        payload["color"] = color
    if is_default is not None:
        payload["is_default"] = is_default
    metadata = _build_metadata(metadata_json, channel, chat_id, session_key)
    if metadata is not None:
        payload["metadata"] = metadata
    calendar = _api_call("PUT", f"/api/v1/calendars/{calendar_id}", payload)["data"]
    typer.echo(f"Updated calendar: {calendar['id']}")
    typer.echo(f"Name: {calendar['name']}")


@calendar_app.command("delete")
def calendar_delete(calendar_id: Annotated[str, typer.Argument(help="Calendar ID")]) -> None:
    """Delete a calendar."""

    _api_call("DELETE", f"/api/v1/calendars/{calendar_id}")
    typer.echo(f"Calendar deleted: {calendar_id}")


@event_app.command("create")
def event_create(
    title: Annotated[str, typer.Argument(help="Event title")],
    time: Annotated[Optional[str], typer.Option("--time", "--start-time", "-t", help="Start time (ISO format, 'now', '+1h')")] = None,
    end_time: Annotated[Optional[str], typer.Option("--end-time", help="End time (ISO format, 'now', '+2h')")] = None,
    duration: Annotated[int, typer.Option("--duration", "-d", help="Duration in minutes if end time is omitted")] = 60,
    venue: Annotated[Optional[str], typer.Option("--venue", "-v", help="Venue/location")] = None,
    calendar_id: Annotated[Optional[str], typer.Option("--calendar-id", "-c", help="Calendar ID")] = None,
    description: Annotated[Optional[str], typer.Option("--description", help="Event description")] = None,
    agenda: Annotated[Optional[str], typer.Option("--agenda", help="Event agenda")] = None,
    timezone: Annotated[str, typer.Option("--timezone", help="Event timezone")] = "Australia/Perth",
    is_all_day: Annotated[bool, typer.Option("--all-day", help="Mark the event as all-day")] = False,
    recurrence_rule: Annotated[Optional[str], typer.Option("--recurrence-rule", help="Recurrence rule")] = None,
    status: Annotated[str, typer.Option("--status", help="Event status")] = "tentative",
) -> None:
    """Create a calendar event."""

    calendar_id = _resolve_calendar_id(calendar_id)
    start = _parse_time_expression(time) if time else datetime.now() + timedelta(hours=1)
    end = _parse_time_expression(end_time) if end_time else start + timedelta(minutes=duration)
    payload: dict[str, Any] = {
        "calendar_id": calendar_id,
        "title": title,
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "timezone": timezone,
        "is_all_day": is_all_day,
        "status": status,
    }
    if description is not None:
        payload["description"] = description
    if agenda is not None:
        payload["agenda"] = agenda
    if venue is not None:
        payload["venue"] = venue
    if recurrence_rule is not None:
        payload["recurrence_rule"] = recurrence_rule
    event = _api_call("POST", "/api/v1/events", payload)["data"]
    typer.echo(f"Created event: {event['id']}")
    typer.echo(f"Title: {event['title']}")
    typer.echo(f"When: {event['start_time'][:16].replace('T', ' ')}")


@event_app.command("list")
def event_list(
    calendar_id: Annotated[Optional[str], typer.Option("--calendar-id", "-c", help="Filter by calendar ID")] = None,
    date_from: Annotated[Optional[str], typer.Option("--from", help="Filter from ISO datetime")] = None,
    date_to: Annotated[Optional[str], typer.Option("--to", help="Filter to ISO datetime")] = None,
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (table, json)")] = "table",
) -> None:
    """List events."""

    events = _api_call(
        "GET",
        f"/api/v1/events{_query_string(calendar_id=calendar_id, date_from=date_from, date_to=date_to)}",
    )["data"]
    if format == "json":
        _echo_json(events)
        return
    if not events:
        typer.echo("No events found.")
        return
    _print_event_table(events)


@event_app.command("get")
def event_get(event_id: Annotated[str, typer.Argument(help="Event ID")]) -> None:
    """Get event details."""

    _echo_json(_api_call("GET", f"/api/v1/events/{event_id}")["data"])


@event_app.command("update")
def event_update(
    event_id: Annotated[str, typer.Argument(help="Event ID")],
    calendar_id: Annotated[Optional[str], typer.Option("--calendar-id", "-c", help="Calendar ID")] = None,
    title: Annotated[Optional[str], typer.Option(help="Event title")] = None,
    description: Annotated[Optional[str], typer.Option("--description", help="Event description")] = None,
    agenda: Annotated[Optional[str], typer.Option("--agenda", help="Event agenda")] = None,
    venue: Annotated[Optional[str], typer.Option("--venue", "-v", help="Venue/location")] = None,
    start_time: Annotated[Optional[str], typer.Option("--start-time", "-t", help="Start time (ISO format, 'now', '+1h')")] = None,
    end_time: Annotated[Optional[str], typer.Option("--end-time", help="End time (ISO format, 'now', '+2h')")] = None,
    timezone: Annotated[Optional[str], typer.Option("--timezone", help="Event timezone")] = None,
    is_all_day: Annotated[Optional[bool], typer.Option("--all-day/--timed", help="Set or unset the all-day flag")] = None,
    recurrence_rule: Annotated[Optional[str], typer.Option("--recurrence-rule", help="Recurrence rule")] = None,
    status: Annotated[Optional[str], typer.Option("--status", help="Event status")] = None,
) -> None:
    """Update an event."""

    payload: dict[str, Any] = {}
    if calendar_id is not None:
        payload["calendar_id"] = calendar_id
    if title is not None:
        payload["title"] = title
    if description is not None:
        payload["description"] = description
    if agenda is not None:
        payload["agenda"] = agenda
    if venue is not None:
        payload["venue"] = venue
    if start_time is not None:
        payload["start_time"] = _time_to_iso(start_time)
    if end_time is not None:
        payload["end_time"] = _time_to_iso(end_time)
    if timezone is not None:
        payload["timezone"] = timezone
    if is_all_day is not None:
        payload["is_all_day"] = is_all_day
    if recurrence_rule is not None:
        payload["recurrence_rule"] = recurrence_rule
    if status is not None:
        payload["status"] = status
    event = _api_call("PUT", f"/api/v1/events/{event_id}", payload)["data"]
    typer.echo(f"Updated event: {event['id']}")
    typer.echo(f"Title: {event['title']}")


@event_app.command("delete")
def event_delete(event_id: Annotated[str, typer.Argument(help="Event ID")]) -> None:
    """Delete an event."""

    _api_call("DELETE", f"/api/v1/events/{event_id}")
    typer.echo(f"Event deleted: {event_id}")


@event_app.command("confirm")
def event_confirm(event_id: Annotated[str, typer.Argument(help="Event ID")]) -> None:
    """Confirm an event."""

    event = _api_call("POST", f"/api/v1/events/{event_id}/confirm")["data"]
    typer.echo(f"Event confirmed: {event['title']}")


@event_app.command("cancel")
def event_cancel(event_id: Annotated[str, typer.Argument(help="Event ID")]) -> None:
    """Cancel an event."""

    event = _api_call("POST", f"/api/v1/events/{event_id}/cancel")["data"]
    typer.echo(f"Event cancelled: {event['title']}")


@event_app.command("recipients")
def event_recipients(
    event_id: Annotated[str, typer.Argument(help="Event ID")],
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (table, json)")] = "table",
) -> None:
    """List event recipients."""

    recipients = _api_call("GET", f"/api/v1/events/{event_id}/recipients")["data"]
    if format == "json":
        _echo_json(recipients)
        return
    if not recipients:
        typer.echo("No recipients found for this event.")
        return
    typer.echo(f"{'ID':<36} {'Type':<10} {'Status':<10} {'Recipient'}")
    typer.echo("-" * 100)
    for recipient in recipients:
        name = recipient.get("name") or recipient["recipient_address"]
        typer.echo(f"{recipient['id']:<36} {recipient['recipient_type']:<10} {recipient['status']:<10} {name[:36]}")


@event_app.command("recipient-add")
def event_recipient_add(
    event_id: Annotated[str, typer.Argument(help="Event ID")],
    recipient_address: Annotated[str, typer.Option("--address", "-a", help="Recipient address")],
    recipient_type: Annotated[str, typer.Option("--type", "-t", help="Recipient type")] = "email",
    name: Annotated[Optional[str], typer.Option("--name", help="Recipient display name")] = None,
    status: Annotated[str, typer.Option("--status", help="Recipient status")] = "pending",
    responded_at: Annotated[Optional[str], typer.Option("--responded-at", help="Response timestamp (ISO format)")] = None,
    notes: Annotated[Optional[str], typer.Option("--notes", help="Recipient notes")] = None,
) -> None:
    """Add a recipient to an event."""

    payload: dict[str, Any] = {
        "recipient_type": recipient_type,
        "recipient_address": recipient_address,
        "status": status,
    }
    if name is not None:
        payload["name"] = name
    if responded_at is not None:
        payload["responded_at"] = responded_at
    if notes is not None:
        payload["notes"] = notes
    recipient = _api_call("POST", f"/api/v1/events/{event_id}/recipients", payload)["data"]
    typer.echo(f"Added recipient: {recipient['id']}")
    typer.echo(f"Address: {recipient['recipient_address']}")


@event_app.command("recipient-update")
def event_recipient_update(
    event_id: Annotated[str, typer.Argument(help="Event ID")],
    recipient_id: Annotated[str, typer.Argument(help="Recipient ID")],
    recipient_type: Annotated[Optional[str], typer.Option("--type", "-t", help="Recipient type")] = None,
    recipient_address: Annotated[Optional[str], typer.Option("--address", "-a", help="Recipient address")] = None,
    name: Annotated[Optional[str], typer.Option("--name", help="Recipient display name")] = None,
    status: Annotated[Optional[str], typer.Option("--status", help="Recipient status")] = None,
    responded_at: Annotated[Optional[str], typer.Option("--responded-at", help="Response timestamp (ISO format)")] = None,
    notes: Annotated[Optional[str], typer.Option("--notes", help="Recipient notes")] = None,
) -> None:
    """Update an event recipient."""

    payload: dict[str, Any] = {}
    if recipient_type is not None:
        payload["recipient_type"] = recipient_type
    if recipient_address is not None:
        payload["recipient_address"] = recipient_address
    if name is not None:
        payload["name"] = name
    if status is not None:
        payload["status"] = status
    if responded_at is not None:
        payload["responded_at"] = responded_at
    if notes is not None:
        payload["notes"] = notes
    recipient = _api_call("PUT", f"/api/v1/events/{event_id}/recipients/{recipient_id}", payload)["data"]
    typer.echo(f"Updated recipient: {recipient['id']}")
    typer.echo(f"Status: {recipient['status']}")


@context_app.command("summary")
def context_summary() -> None:
    """Get full context summary."""

    ctx = _api_call("GET", "/api/v1/context/summary")["data"]
    typer.echo("=" * 60)
    typer.echo("BOB'S CONTEXT SUMMARY")
    typer.echo("=" * 60)
    typer.echo(f"Generated: {ctx['generated_at']}")
    typer.echo()

    typer.echo("TASKS:")
    for status, count in ctx["task_counts"].items():
        typer.echo(f"  {status}: {count}")
    typer.echo()

    typer.echo("PROJECTS:")
    for state, count in ctx["project_counts"].items():
        typer.echo(f"  {state}: {count}")
    typer.echo()

    if ctx["active_tasks"]:
        typer.echo("ACTIVE TASKS:")
        for task in ctx["active_tasks"]:
            if task.get("parent_project_id") and task.get("parent_project_title"):
                typer.echo(
                    f"  * {task['title']} ({task['priority']}) "
                    f"[project: {task['parent_project_title']} / {task['parent_project_id']}]"
                )
            else:
                typer.echo(f"  * {task['title']} ({task['priority']})")
        typer.echo()

    if ctx["active_projects"]:
        typer.echo("ACTIVE PROJECTS:")
        for project in ctx["active_projects"]:
            typer.echo(f"  * {project['title']}")
        typer.echo()

    if ctx["upcoming_events"]:
        typer.echo("UPCOMING EVENTS:")
        for event in ctx["upcoming_events"][:5]:
            start = event["start_time"][:16].replace("T", " ")
            typer.echo(f"  * {start} - {event['title']}")


@context_app.command("tasks")
def context_tasks() -> None:
    """Get task-focused context."""

    _echo_json(_api_call("GET", "/api/v1/context/tasks")["data"])


@context_app.command("projects")
def context_projects() -> None:
    """Get project-focused context."""

    _echo_json(_api_call("GET", "/api/v1/context/projects")["data"])


@context_app.command("calendar")
def context_calendar() -> None:
    """Get calendar-focused context."""

    _echo_json(_api_call("GET", "/api/v1/context/calendar")["data"])


@webhook_app.command("create")
def webhook_create(
    name: Annotated[str, typer.Argument(help="Webhook name")],
    url: Annotated[str, typer.Option("--url", help="Webhook target URL")],
    secret: Annotated[str, typer.Option("--secret", help="Webhook signing secret")],
    events: Annotated[Optional[list[str]], typer.Option("--event", help="Webhook event; repeat to add more")] = None,
    retry_count: Annotated[int, typer.Option("--retry-count", help="Webhook retry count")] = 3,
) -> None:
    """Create a webhook configuration."""

    if not events:
        raise typer.BadParameter("At least one --event value is required")
    webhook = _api_call(
        "POST",
        "/api/v1/webhooks",
        {"name": name, "url": url, "secret": secret, "events": events, "retry_count": retry_count},
    )["data"]
    typer.echo(f"Created webhook: {webhook['id']}")
    typer.echo(f"Name: {webhook['name']}")


@webhook_app.command("list")
def webhook_list(
    active_only: Annotated[bool, typer.Option("--active-only/--include-inactive", help="List only active webhooks")] = True,
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (table, json)")] = "table",
) -> None:
    """List webhook configurations."""

    webhooks = _api_call("GET", f"/api/v1/webhooks{_query_string(active_only=str(active_only).lower())}")["data"]
    if format == "json":
        _echo_json(webhooks)
        return
    if not webhooks:
        typer.echo("No webhooks found.")
        return
    typer.echo(f"{'ID':<36} {'Active':<8} {'Name'}")
    typer.echo("-" * 80)
    for webhook in webhooks:
        active = "yes" if webhook["is_active"] else "no"
        typer.echo(f"{webhook['id']:<36} {active:<8} {webhook['name']}")


@webhook_app.command("get")
def webhook_get(config_id: Annotated[str, typer.Argument(help="Webhook configuration ID")]) -> None:
    """Get a webhook configuration by ID."""

    _echo_json(_api_call("GET", f"/api/v1/webhooks/{config_id}")["data"])


@webhook_app.command("by-name")
def webhook_get_by_name(name: Annotated[str, typer.Argument(help="Webhook name")]) -> None:
    """Get a webhook configuration by name."""

    _echo_json(_api_call("GET", f"/api/v1/webhooks/by-name/{name}")["data"])


@webhook_app.command("update")
def webhook_update(
    config_id: Annotated[str, typer.Argument(help="Webhook configuration ID")],
    url: Annotated[Optional[str], typer.Option("--url", help="Webhook target URL")] = None,
    secret: Annotated[Optional[str], typer.Option("--secret", help="Webhook signing secret")] = None,
    events: Annotated[Optional[list[str]], typer.Option("--event", help="Webhook event; repeat to add more")] = None,
    retry_count: Annotated[Optional[int], typer.Option("--retry-count", help="Webhook retry count")] = None,
    is_active: Annotated[Optional[bool], typer.Option("--active/--inactive", help="Activate or deactivate the webhook")] = None,
) -> None:
    """Update a webhook configuration."""

    payload: dict[str, Any] = {}
    if url is not None:
        payload["url"] = url
    if secret is not None:
        payload["secret"] = secret
    if events:
        payload["events"] = events
    if retry_count is not None:
        payload["retry_count"] = retry_count
    if is_active is not None:
        payload["is_active"] = is_active
    webhook = _api_call("PUT", f"/api/v1/webhooks/{config_id}", payload)["data"]
    typer.echo(f"Updated webhook: {webhook['id']}")
    typer.echo(f"Name: {webhook['name']}")


@webhook_app.command("delete")
def webhook_delete(config_id: Annotated[str, typer.Argument(help="Webhook configuration ID")]) -> None:
    """Delete a webhook configuration."""

    _api_call("DELETE", f"/api/v1/webhooks/{config_id}")
    typer.echo(f"Webhook deleted: {config_id}")


@webhook_app.command("deliveries")
def webhook_deliveries(
    webhook_id: Annotated[Optional[str], typer.Option("--webhook-id", help="Filter by webhook ID")] = None,
    status: Annotated[Optional[str], typer.Option("--status", help="Filter by delivery status")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Maximum number of deliveries")] = 100,
    offset: Annotated[int, typer.Option("--offset", help="Result offset")] = 0,
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (table, json)")] = "table",
) -> None:
    """List webhook deliveries."""

    deliveries = _api_call(
        "GET",
        f"/api/v1/webhooks/deliveries{_query_string(webhook_id=webhook_id, status=status, limit=limit, offset=offset)}",
    )["data"]
    if format == "json":
        _echo_json(deliveries)
        return
    if not deliveries:
        typer.echo("No deliveries found.")
        return
    typer.echo(f"{'ID':<36} {'Status':<12} {'Event':<24} {'Attempt'}")
    typer.echo("-" * 100)
    for delivery in deliveries:
        event_name = str(delivery.get("event", ""))[:24]
        attempt = delivery.get("attempt_count", "")
        typer.echo(f"{delivery['id']:<36} {delivery.get('status', ''):<12} {event_name:<24} {attempt}")


@webhook_app.command("delivery-get")
def webhook_delivery_get(delivery_id: Annotated[str, typer.Argument(help="Delivery ID")]) -> None:
    """Get a webhook delivery by ID."""

    _echo_json(_api_call("GET", f"/api/v1/webhooks/deliveries/{delivery_id}")["data"])


@webhook_app.command("delivery-retry")
def webhook_delivery_retry(delivery_id: Annotated[str, typer.Argument(help="Delivery ID")]) -> None:
    """Retry a failed webhook delivery."""

    result = _api_call("POST", f"/api/v1/webhooks/deliveries/{delivery_id}/retry")["data"]
    typer.echo(f"Retried delivery: {delivery_id}")
    typer.echo(f"Success: {result['success']}")


@webhook_app.command("process-pending")
def webhook_process_pending() -> None:
    """Process pending webhook deliveries."""

    result = _api_call("POST", "/api/v1/webhooks/process-pending")["data"]
    typer.echo(f"Processed deliveries: {result['processed']}")


@openclaw_app.command("context")
def openclaw_context(
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (text, json)")] = "text",
) -> None:
    """Fetch OpenClaw context payload."""

    if format == "text":
        typer.echo(_text_call("/openclaw/context.txt"))
        return
    if format == "json":
        _echo_json(_api_call("GET", "/openclaw/context.json")["data"])
        return
    raise typer.BadParameter("format must be either 'text' or 'json'")


# ============================================================================
# Health Commands
# ============================================================================


@health_app.command("scan")
def health_scan(
    include_healthy: Annotated[bool, typer.Option("--include-healthy", help="Include healthy projects in results")] = False,
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (table, json)")] = "table",
) -> None:
    """Scan all active projects for health issues.

    Returns projects with health assessments, sorted by risk level.
    """

    result = _api_call("GET", f"/api/v1/health/scan{_query_string(include_healthy=include_healthy)}")["data"]

    if format == "json":
        _echo_json(result)
        return

    typer.echo(f"Scanned {result['scanned_count']}/{result['total_projects']} active projects")
    typer.echo(f"Timestamp: {result['timestamp']}")
    typer.echo()

    if not result["projects"]:
        typer.echo("No projects found.")
        return

    # Sort by risk level
    risk_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    projects = sorted(result["projects"], key=lambda p: (risk_order.get(p["risk_level"], 2), -p["health_score"]))

    typer.echo(f"{'Project ID':<36} {'Title':<30} {'Score':<6} {'Risk'}")
    typer.echo("-" * 90)

    for p in projects:
        title = (p.get("project_title") or "")[:28]
        score = f"{p.get('health_score', 0):.2f}"
        risk = p.get("risk_level", "unknown")
        risk_emoji = {
            "low": "✓",
            "medium": "⚠",
            "high": "⚠⚠",
            "critical": "🚨",
        }.get(risk, "?")
        typer.echo(f"{p['project_id']:<36} {title:<30} {score:<6} {risk_emoji} {risk}")


@health_app.command("analyze")
def health_analyze(
    project_id: Annotated[str, typer.Option("--project", "-p", help="Project ID")],
    save: Annotated[bool, typer.Option("--save", "-s", help="Save the health check to database")] = False,
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (text, json)")] = "text",
) -> None:
    """Get health analysis for a specific project."""

    if not project_id:
        raise typer.BadParameter("--project is required")

    result = _api_call("GET", f"/api/v1/health/projects/{project_id}/health{_query_string(save_check=save)}")["data"]

    if format == "json":
        _echo_json(result)
        return

    typer.echo(f"Health Analysis for Project: {result['project_id']}")
    if result.get("project_title"):
        typer.echo(f"Title: {result['project_title']}")
    typer.echo()

    risk = result["risk_level"]
    risk_emoji = {"low": "✓", "medium": "⚠", "high": "⚠⚠", "critical": "🚨"}.get(risk, "?")
    typer.echo(f"Health Score: {result['health_score']:.2f}/1.0")
    typer.echo(f"Risk Level: {risk_emoji} {risk.upper()}")
    typer.echo()

    # Indicators
    indicators = result.get("indicators", {})
    if indicators:
        typer.echo("Indicators:")
        for key, value in indicators.items():
            if key == "blocker_details" and value:
                typer.echo(f"  Blocked Tasks:")
                for blocker in value:
                    reason = blocker.get("reason") or "No reason provided"
                    typer.echo(f"    - {blocker['task_title']}: {reason}")
            elif isinstance(value, (int, float)):
                typer.echo(f"  {key}: {value}")
            elif isinstance(value, float):
                typer.echo(f"  {key}: {value:.2%}")
        typer.echo()

    # Recommendations
    recommendations = result.get("recommendations", [])
    if recommendations:
        typer.echo("Recommendations:")
        for rec in recommendations:
            priority = rec.get("priority", "info").upper()
            action = rec.get("action", "No action specified")
            reason = rec.get("reason", "")
            typer.echo(f"  [{priority}] {action}")
            if reason:
                typer.echo(f"      Reason: {reason}")
        typer.echo()

    if result.get("analysis_timestamp"):
        typer.echo(f"Analyzed at: {result['analysis_timestamp']}")


@health_app.command("projects-needing-attention")
def health_projects_needing_attention(
    limit: Annotated[int, typer.Option("--limit", "-l", help="Maximum projects to return")] = 20,
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (table, json)")] = "table",
) -> None:
    """Get projects that need attention (high/critical risk or alerts).

    Returns projects sorted by urgency.
    """

    projects = _api_call("GET", f"/api/v1/health/projects-needing-attention{_query_string(limit=limit)}")["data"]

    if format == "json":
        _echo_json(projects)
        return

    if not projects:
        typer.echo("No projects need attention right now.")
        return

    typer.echo(f"Found {len(projects)} project(s) needing attention")
    typer.echo()

    typer.echo(f"{'Project ID':<36} {'Title':<30} {'State':<10} {'Score'}")
    typer.echo("-" * 100)

    for p in projects:
        title = p.get("title", "")[:28]
        state = p.get("state", "")[:8]
        score = f"{p.get('health_score', 0):.2f}"
        alert = " 🚨" if p.get("alert_triggered") else ""
        typer.echo(f"{p['project_id']:<36} {title:<30} {state:<10} {score}{alert}")


@health_app.command("latest")
def health_latest(
    project_id: Annotated[str, typer.Option("--project", "-p", help="Project ID")],
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (text, json)")] = "text",
) -> None:
    """Get the most recent health check for a project."""

    if not project_id:
        raise typer.BadParameter("--project is required")

    result = _api_call("GET", f"/api/v1/health/projects/{project_id}/health/latest")["data"]

    if "detail" in result and "No health checks found" in result["detail"]:
        typer.echo(f"No health checks found for project {project_id}")
        return

    if format == "json":
        _echo_json(result)
        return

    typer.echo(f"Latest Health Check for Project: {result['project_id']}")
    typer.echo(f"Check Type: {result.get('check_type', 'unknown')}")
    typer.echo(f"Health Score: {result.get('health_score', 0):.2f}/1.0")
    typer.echo(f"Risk Level: {result.get('risk_level', 'unknown').upper()}")
    typer.echo(f"Alert Triggered: {'Yes' if result.get('alert_triggered') else 'No'}")
    typer.echo(f"Checked at: {result.get('created_at', 'unknown')}")
    typer.echo()

    if result.get("recommendations"):
        typer.echo("Recommendations:")
        for rec in result.get("recommendations", []):
            typer.echo(f"  - {rec}")


# ============================================================================
# Learning Commands
# ============================================================================


@learning_app.command("extract-insights")
def learning_extract_insights(
    project_id: Annotated[str, typer.Option("--project", "-p", help="Project ID")],
    force: Annotated[bool, typer.Option("--force", "-f", help="Extract even if project is young")] = False,
    format: Annotated[str, typer.Option("--format", help="Output format (text, json)")] = "text",
) -> None:
    """Extract and store insights from a completed project.

    Uses OpenClaw reasoning to analyze the project and identify learnings.
    """

    if not project_id:
        raise typer.BadParameter("--project is required")

    payload = {"force": force}
    result = _api_call("POST", f"/api/v1/learning/projects/{project_id}/extract-insights", payload)["data"]

    if format == "json":
        _echo_json(result)
        return

    typer.echo(f"Extracted {result['insights_extracted']} insight(s) from project {project_id}")
    typer.echo()

    insights = result.get("insights", [])
    if insights:
        for i, insight in enumerate(insights, 1):
            typer.echo(f"{i}. {insight.get('category', 'Uncategorized')}")
            typer.echo(f"   {insight.get('insight', 'No insight text')}")
            if insight.get("applicability_pattern"):
                typer.echo(f"   Applies to: {insight['applicability_pattern']}")
            typer.echo()


@learning_app.command("similar-projects")
def learning_similar_projects(
    aim: Annotated[str, typer.Option("--aim", "-a", help="Project aim to match against")],
    method: Annotated[Optional[str], typer.Option("--method", "-m", help="Project method to match")] = None,
    limit: Annotated[int, typer.Option("--limit", "-l", help="Maximum projects to return")] = 5,
    min_outcome: Annotated[Optional[str], typer.Option("--outcome", help="Filter by outcome: success, failure, partial")] = None,
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (table, json)")] = "table",
) -> None:
    """Find projects similar to the given aim/method.

    Returns projects with their insights, ordered by relevance.
    """

    if not aim:
        raise typer.BadParameter("--aim is required")

    result = _api_call(
        "GET",
        f"/api/v1/learning/similar-projects{_query_string(aim=aim, method=method, limit=limit, min_outcome=min_outcome)}",
    )["data"]

    if format == "json":
        _echo_json(result)
        return

    typer.echo(f"Found {result['total_found']} similar project(s)")
    typer.echo()

    if not result["projects"]:
        typer.echo("No similar projects found.")
        return

    for i, project in enumerate(result["projects"], 1):
        typer.echo(f"{i}. {project.get('title', 'Untitled')} [{project['id'][:8]}]")
        typer.echo(f"   Aim: {project.get('aim', '')[:60]}")
        if project.get("method"):
            typer.echo(f"   Method: {project['method'][:60]}")
        typer.echo(f"   Outcome: {project.get('outcome', 'unknown').upper()}")
        if project.get("insights"):
            typer.echo(f"   Insights: {len(project['insights'])} available")
        typer.echo()


@learning_app.command("active-insights")
def learning_active_insights(
    category: Annotated[Optional[str], typer.Option("--category", "-c", help="Filter by insight category")] = None,
    limit: Annotated[int, typer.Option("--limit", "-l", help="Maximum insights to return")] = 50,
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (text, json)")] = "text",
) -> None:
    """Get active (successful/partial) insights that can be applied to new projects.

    Insights are extracted from completed successful projects.
    """

    result = _api_call(
        "GET",
        f"/api/v1/learning/insights/active{_query_string(category=category, limit=limit)}",
    )["data"]

    if format == "json":
        _echo_json(result)
        return

    typer.echo(f"Found {result['total']} active insight(s)")
    typer.echo()

    if not result["insights"]:
        typer.echo("No active insights found.")
        return

    # Group by category
    by_category: dict[str, list[dict[str, Any]]] = {}
    for insight in result["insights"]:
        cat = insight.get("category", "General")
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(insight)

    for category, insights in sorted(by_category.items()):
        typer.echo(f"[{category}]")
        for insight in insights:
            typer.echo(f"  - {insight.get('insight', 'No insight text')}")
            if insight.get("applicability_pattern"):
                typer.echo(f"    Applies to: {insight['applicability_pattern']}")
        typer.echo()


@learning_app.command("suggest-criteria")
def learning_suggest_criteria(
    aim: Annotated[str, typer.Option("--aim", "-a", help="Project aim to match against")],
    method: Annotated[Optional[str], typer.Option("--method", "-m", help="Project method to match")] = None,
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (text, json)")] = "text",
) -> None:
    """Suggest success criteria based on similar successful projects.

    Returns criteria from similar past projects.
    """

    if not aim:
        raise typer.BadParameter("--aim is required")

    result = _api_call(
        "GET",
        f"/api/v1/learning/suggest-criteria{_query_string(aim=aim, method=method)}",
    )["data"]

    if format == "json":
        _echo_json(result)
        return

    criteria = result.get("criteria", [])

    if not criteria:
        typer.echo("No similar projects found to suggest criteria.")
        return

    typer.echo(f"Suggested success criteria based on {len(criteria)} similar project(s)")
    typer.echo()

    for i, criterion in enumerate(criteria, 1):
        source = criterion.get("source_project_title", "Unknown project")
        typer.echo(f"{i}. {criterion.get('criterion', 'No criterion text')}")
        typer.echo(f"   Source: {source}")
        if criterion.get("met"):
            typer.echo(f"   ✓ This criterion was met")
        else:
            typer.echo(f"   ✗ This criterion was NOT met")
        typer.echo()


# ---------------------------------------------------------------------------
# Email relay
# ---------------------------------------------------------------------------

email_app = typer.Typer(help="Email relay operations")
email_inbox_app = typer.Typer(help="Email inbox management")
email_app.add_typer(email_inbox_app, name="inbox")

app.add_typer(email_app, name="email")


def _read_file_as_attachment(file_path: str, *, inline: bool = False) -> dict[str, Any]:
    path = Path(file_path)
    if not path.is_file():
        raise typer.BadParameter(f"File not found: {file_path}")
    content = base64.b64encode(path.read_bytes()).decode("ascii")
    result: dict[str, Any] = {
        "content": content,
        "filename": path.name,
        "content_type": mimetypes.guess_type(str(path))[0] or "application/octet-stream",
        "content_disposition": "inline" if inline else "attachment",
    }
    if inline:
        result["content_id"] = path.name
    return result


@email_inbox_app.command("register")
def email_inbox_register(
    agentmail_inbox_id: Annotated[str, typer.Option("--agentmail-inbox-id", help="AgentMail inbox ID")],
    display_name: Annotated[str, typer.Option("--display-name", help="Display name for this inbox")],
    email_address: Annotated[str, typer.Option("--email-address", help="Email address for this inbox")],
    metadata_json: Annotated[Optional[str], typer.Option("--metadata-json", help="Optional metadata JSON")] = None,
) -> None:
    """Register an AgentMail inbox for email relay."""
    payload: dict[str, Any] = {
        "agentmail_inbox_id": agentmail_inbox_id,
        "display_name": display_name,
        "email_address": email_address,
    }
    if metadata_json:
        payload["metadata"] = _parse_json_option(metadata_json, "metadata-json", dict)
    result = _api_call("POST", "/api/v1/email/inboxes", payload)
    _echo_json(result["data"])


@email_inbox_app.command("list")
def email_inbox_list() -> None:
    """List registered email inboxes."""
    result = _api_call("GET", "/api/v1/email/inboxes")
    _echo_json(result["data"])


@email_inbox_app.command("get")
def email_inbox_get(
    id: Annotated[str, typer.Argument(help="Inbox ID")],
) -> None:
    """Get a registered email inbox."""
    result = _api_call("GET", f"/api/v1/email/inboxes/{id}")
    _echo_json(result["data"])


@email_inbox_app.command("remove")
def email_inbox_remove(
    id: Annotated[str, typer.Argument(help="Inbox ID")],
) -> None:
    """Remove a registered email inbox."""
    _api_call("DELETE", f"/api/v1/email/inboxes/{id}")
    typer.echo("Inbox removed.")


@email_app.command("send")
def email_send(
    to: Annotated[str, typer.Option("--to", help="Recipient email address")],
    subject: Annotated[str, typer.Option("--subject", help="Email subject")],
    text: Annotated[str, typer.Option("--text", help="Email body text")],
    agenda: Annotated[str, typer.Option("--agenda", help="Purpose and handling instructions for this email thread (required)")],
    cc: Annotated[Optional[list[str]], typer.Option("--cc", help="CC recipients")] = None,
    html: Annotated[Optional[str], typer.Option("--html", help="HTML body (use cid: references for inline images)")] = None,
    attach: Annotated[Optional[list[str]], typer.Option("--attach", help="File path to attach (repeatable)")] = None,
    inline_image: Annotated[Optional[list[str]], typer.Option("--inline-image", help="Inline image file path (repeatable)")] = None,
    inbox_id: Annotated[Optional[str], typer.Option("--inbox", help="Inbox ID (default: auto-resolve)")] = None,
) -> None:
    """Send a new email from a registered inbox."""
    resolved_inbox = _resolve_inbox_id(inbox_id)
    payload: dict[str, Any] = {"to": to, "subject": subject, "text": text, "agenda": agenda}
    if cc:
        payload["cc"] = cc
    if html:
        payload["html"] = html
    attachments: list[dict[str, Any]] = []
    if attach:
        for fp in attach:
            attachments.append(_read_file_as_attachment(fp))
    if inline_image:
        for fp in inline_image:
            attachments.append(_read_file_as_attachment(fp, inline=True))
    if attachments:
        payload["attachments"] = attachments
    result = _api_call("POST", f"/api/v1/email/inboxes/{resolved_inbox}/send", payload)
    _echo_json(result.get("data", result))


@email_app.command("reply")
def email_reply(
    message_id: Annotated[str, typer.Option("--message-id", help="Message ID to reply to")],
    text: Annotated[str, typer.Option("--text", help="Reply body text")],
    reply_all: Annotated[bool, typer.Option("--reply-all", help="Reply to all recipients")] = False,
    html: Annotated[Optional[str], typer.Option("--html", help="HTML body (use cid: references for inline images)")] = None,
    attach: Annotated[Optional[list[str]], typer.Option("--attach", help="File path to attach (repeatable)")] = None,
    inline_image: Annotated[Optional[list[str]], typer.Option("--inline-image", help="Inline image file path (repeatable)")] = None,
    inbox_id: Annotated[Optional[str], typer.Option("--inbox", help="Inbox ID (default: auto-resolve)")] = None,
) -> None:
    """Reply to an email message."""
    resolved_inbox = _resolve_inbox_id(inbox_id)
    payload: dict[str, Any] = {"message_id": message_id, "text": text, "reply_all": reply_all}
    if html:
        payload["html"] = html
    attachments: list[dict[str, Any]] = []
    if attach:
        for fp in attach:
            attachments.append(_read_file_as_attachment(fp))
    if inline_image:
        for fp in inline_image:
            attachments.append(_read_file_as_attachment(fp, inline=True))
    if attachments:
        payload["attachments"] = attachments
    result = _api_call("POST", f"/api/v1/email/inboxes/{resolved_inbox}/reply", payload)
    _echo_json(result.get("data", result))


@email_app.command("messages")
def email_messages(
    limit: Annotated[int, typer.Option("--limit", help="Max messages")] = 25,
    inbox_id: Annotated[Optional[str], typer.Option("--inbox", help="Inbox ID (default: auto-resolve)")] = None,
) -> None:
    """List messages in an inbox."""
    resolved_inbox = _resolve_inbox_id(inbox_id)
    result = _api_call("GET", f"/api/v1/email/inboxes/{resolved_inbox}/messages?limit={limit}")
    _echo_json(result.get("data", result))


@email_app.command("download-attachment")
def email_download_attachment(
    message_id: Annotated[str, typer.Option("--message-id", help="AgentMail message ID")],
    attachment_id: Annotated[str, typer.Option("--attachment-id", help="Attachment ID")],
    output: Annotated[str, typer.Option("--output", "-o", help="Output file path")] = "",
    inbox_id: Annotated[Optional[str], typer.Option("--inbox", help="Inbox ID (default: auto-resolve)")] = None,
) -> None:
    """Download an email attachment to disk."""
    resolved_inbox = _resolve_inbox_id(inbox_id)
    settings = Settings.from_env()
    encoded_msg = quote(message_id, safe="")
    url = f"http://{settings.host}:{settings.port}/api/v1/email/inboxes/{resolved_inbox}/messages/{encoded_msg}/attachments/{attachment_id}"
    req = Request(url, method="GET")

    try:
        with urlopen(req, timeout=60) as response:
            content = response.read()
    except HTTPError as exc:
        _handle_http_error(exc)
    except URLError as exc:
        _handle_connection_error(exc)

    output_path = Path(output) if output else Path(attachment_id)
    if output and output_path.is_dir():
        output_path = output_path / attachment_id
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(content)
    _echo_json({"path": str(output_path.resolve()), "size": len(content)})


@email_app.command("threads")
def email_threads(
    inbox_id: Annotated[Optional[str], typer.Option("--inbox", help="Filter by inbox ID")] = None,
) -> None:
    """List tracked email threads."""
    qs = _query_string(inbox_id=inbox_id)
    result = _api_call("GET", f"/api/v1/email/threads{qs}")
    _echo_json(result["data"])


@email_app.command("thread")
def email_thread_get(
    thread_id: Annotated[str, typer.Argument(help="Thread ID")],
) -> None:
    """Get a tracked email thread."""
    result = _api_call("GET", f"/api/v1/email/threads/{thread_id}")
    _echo_json(result["data"])


@email_app.command("update-agenda")
def email_thread_update_agenda(
    thread_id: Annotated[str, typer.Argument(help="Thread ID")],
    agenda: Annotated[str, typer.Option("--agenda", help="New agenda text for the thread")],
) -> None:
    """Update the agenda for an email thread."""
    result = _api_call("PATCH", f"/api/v1/email/threads/{thread_id}/agenda", {"agenda": agenda})
    _echo_json(result.get("data", result))


@email_app.command("sync")
def email_sync() -> None:
    """Sync all inboxes — fetch missing messages from AgentMail and persist locally."""
    result = _api_call("POST", "/api/v1/email/sync")
    data = result.get("data", result)
    count = data.get("synced", 0)
    typer.echo(f"Synced {count} message(s) from AgentMail")


@phone_app.command("call")
def phone_call(
    to: Annotated[str, typer.Argument(help="Phone number to call (E.164 format, e.g. +1234567890)")],
    agenda: Annotated[str, typer.Option("--agenda", help="Purpose and handling instructions for the call")],
) -> None:
    """Initiate an outbound phone call with an agenda for the voice assistant."""
    result = _api_call("POST", "/phone/call", {"to": to, "agenda": agenda})
    _echo_json(result.get("data", result))


@phone_app.command("list")
def phone_list(
    limit: Annotated[int, typer.Option("--limit", help="Max calls to return")] = 20,
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (table, json)")] = "table",
) -> None:
    """List recent phone calls."""
    result = _api_call("GET", "/phone/calls")
    calls = result.get("data", result).get("calls", [])
    if format == "json":
        _echo_json(calls)
        return
    if not calls:
        typer.echo("No calls found.")
        return
    for c in calls:
        sid = c.get("call_sid", "")[:12]
        status = c.get("status", "")
        started = c.get("started_at", "")
        exchanges = c.get("exchange_count", 0)
        duration = c.get("duration_seconds")
        dur_str = f"{duration:.0f}s" if duration else "—"
        has_recording = "Y" if c.get("recording_path") else "—"
        typer.echo(f"{sid}  {status:<10}  {started}  {exchanges} exchanges  {dur_str}  rec:{has_recording}")


@phone_app.command("status")
def phone_status(
    call_id: Annotated[str, typer.Argument(help="Call SID or internal ID")],
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (text, json)")] = "text",
) -> None:
    """Get call status, transcript, and latency details."""
    result = _api_call("GET", f"/phone/calls/{call_id}")
    data = result.get("data", result)
    if "error" in data:
        typer.echo(f"Error: {data['error']}", err=True)
        raise typer.Exit(code=1)

    if format == "json":
        _echo_json(data)
        return

    call = data.get("call", {})
    exchanges = data.get("exchanges", [])

    typer.echo(f"Status:    {call.get('status')}")
    typer.echo(f"Started:   {call.get('started_at')}")
    typer.echo(f"Completed: {call.get('completed_at') or '—'}")
    typer.echo(f"Duration:  {'%.0fs' % call['duration_seconds'] if call.get('duration_seconds') else '—'}")
    typer.echo(f"Exchanges: {call.get('exchange_count', 0)}")
    typer.echo(f"Recording: {'Yes' if call.get('recording_path') else 'No'}")
    if call.get("agenda"):
        typer.echo(f"Agenda:    {call['agenda'][:120]}{'...' if len(call['agenda']) > 120 else ''}")
    typer.echo()

    if not exchanges:
        typer.echo("No exchanges yet.")
        return

    for ex in exchanges:
        idx = ex.get("exchange_index", 0)
        user = ex.get("user_transcript", "")
        assistant = ex.get("assistant_transcript", "")
        e2e = ex.get("e2e_ms")
        typer.echo(f"--- Exchange #{idx + 1} ---")
        typer.echo(f"  User:      {user or '—'}")
        typer.echo(f"  Assistant: {assistant or '(no response)'}")
        if e2e:
            typer.echo(f"  Latency:   STT {ex.get('stt_ms', '—')}ms | LLM {ex.get('openclaw_ms', '—')}ms | TTFP {ex.get('tts_first_chunk_ms', '—')}ms | E2E {e2e}ms")
        typer.echo()


@openai_app.command("prompt")
def openai_prompt(
    prompt: Annotated[str, typer.Argument(help="Prompt text to send")],
    model: Annotated[Optional[str], typer.Option("--model", "-m", help="Model name")] = None,
    temperature: Annotated[float, typer.Option("--temperature", "-t", help="Sampling temperature")] = 0.7,
) -> None:
    """Send a prompt to OpenAI and print the response."""
    data: dict[str, Any] = {"prompt": prompt, "temperature": temperature}
    if model:
        data["model"] = model
    result = _api_call("POST", "/api/v1/openai/prompt", data)
    typer.echo(result["data"]["content"])


# ── Eval framework ──────────────────────────────────────────────────


@eval_app.command("list")
def eval_list(
    category: Annotated[Optional[str], typer.Option("--category", "-c")] = None,
) -> None:
    """List available eval cases."""
    import asyncio
    asyncio.run(_eval_list(category))


async def _eval_list(category: str | None) -> None:
    from cyborg_server.evals.registry import get_all_cases, get_cases_by_category
    cases = get_cases_by_category(category) if category else get_all_cases()
    if not cases:
        typer.echo("No eval cases found.")
        return
    typer.echo(f"{'ID':<40} {'Category':<20} Description")
    typer.echo("-" * 90)
    for c in cases:
        typer.echo(f"{c.id:<40} {c.category:<20} {c.description}")


@eval_app.command("run")
def eval_run(
    category: Annotated[Optional[str], typer.Option("--category", "-c")] = None,
    case_id: Annotated[Optional[str], typer.Option("--case")] = None,
    threshold: Annotated[float, typer.Option("--threshold", "-t")] = 0.7,
    skip_judge: Annotated[bool, typer.Option("--skip-judge")] = False,
) -> None:
    """Run eval cases against live LLM APIs."""
    import asyncio
    asyncio.run(_eval_run(category, case_id, threshold, skip_judge))


async def _eval_run(
    category: str | None,
    case_id: str | None,
    threshold: float,
    skip_judge: bool,
) -> None:
    from cyborg_server.config import Settings
    from cyborg_server.context import AppContext
    from cyborg_server.database import Database

    settings = Settings.from_env()
    schema_dir = Path(__file__).parent / "schemas"
    db_path = settings.db_path or Path("cyborg.db")
    db = Database(db_path, schema_dir)
    await db.connect()
    ctx = AppContext(settings=settings, db=db)

    try:
        from cyborg_server.evals.runner import EvalRunner
        runner = EvalRunner(ctx)
        results = await runner.run_all(
            category=category,
            case_id=case_id,
            judge_threshold=threshold,
            skip_judge=skip_judge,
        )

        if not results:
            typer.echo("No eval cases matched.")
            return

        typer.echo(f"\n{'ID':<35} {'PASS':<6} {'Struct':<8} {'Judge':<8} Latency")
        typer.echo("-" * 75)
        for r in results:
            struct_pass = sum(1 for s in r.structural_results if s.passed)
            struct_total = len(r.structural_results)
            judge_str = f"{r.judge_result.overall:.1f}" if r.judge_result else "skip"
            status = "PASS" if r.passed else "FAIL"
            typer.echo(
                f"{r.case_id:<35} {status:<6} "
                f"{struct_pass}/{struct_total:<6} {judge_str:<8} "
                f"{r.llm_latency_seconds:.1f}s"
            )
            if r.error_message:
                typer.echo(f"  Error: {r.error_message}")

        passed = sum(1 for r in results if r.passed)
        typer.echo(f"\n{passed}/{len(results)} passed")

        if passed < len(results):
            raise SystemExit(1)
    finally:
        await db.close()


@eval_app.command("history")
def eval_history(
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
) -> None:
    """Show historical eval run results."""
    import asyncio
    asyncio.run(_eval_history(limit))


async def _eval_history(limit: int) -> None:
    from cyborg_server.config import Settings
    from cyborg_server.database import Database

    settings = Settings.from_env()
    schema_dir = Path(__file__).parent / "schemas"
    db_path = settings.db_path or Path("cyborg.db")
    db = Database(db_path, schema_dir)
    await db.connect()
    try:
        rows = await db.fetch_all(
            "SELECT * FROM eval_runs WHERE status='completed' "
            "ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
        if not rows:
            typer.echo("No eval runs found.")
            return
        typer.echo(f"{'Run ID':<38} {'Started':<22} {'Cat':<15} {'Pass':>5}/{'<5'} Rate")
        typer.echo("-" * 95)
        for r in rows:
            ts = r["started_at"][:19].replace("T", " ")
            cat = r.get("category") or "all"
            rate = f"{r['overall_pass_rate']:.0%}" if r["overall_pass_rate"] else "N/A"
            typer.echo(
                f"{r['id']:<38} {ts:<22} {cat:<15} "
                f"{r['passed_cases']:>5}/{r['total_cases']:<5} {rate}"
            )
    finally:
        await db.close()


# ============================================================================
# WhatsApp commands
# ============================================================================

whatsapp_app = typer.Typer(help="WhatsApp bridge operations")


@whatsapp_app.command("status")
def whatsapp_status() -> None:
    """Show WhatsApp bridge connection status."""
    result = _api_call("GET", "/whatsapp/status")
    _echo_json(result)


@whatsapp_app.command("pair")
def whatsapp_pair(
    method: Annotated[str, typer.Option("--method", help="Pairing method: 'qr' or 'phone-code'")] = "qr",
    phone_number: Annotated[Optional[str], typer.Option("--phone-number", help="Phone number for phone-code pairing (E.164 format)")] = None,
) -> None:
    """Request WhatsApp device pairing via QR code or phone number code."""
    if method == "phone-code" and not phone_number:
        raise typer.BadParameter("--phone-number is required for phone-code pairing")
    payload = {"method": method}
    if phone_number:
        payload["phone_number"] = phone_number
    result = _api_call("POST", "/whatsapp/pair", payload)

    # Poll for the QR/pairing code
    typer.echo("Waiting for pairing info...")
    for _ in range(10):
        time.sleep(1)
        status = _api_call("GET", "/whatsapp/bridge-status").get("data", {})
        if method == "qr" and status.get("last_qr_code"):
            qr = qrcode.QRCode(border=1)
            qr.add_data(status["last_qr_code"])
            qr.make(fit=True)
            qr.print_ascii(sys.stdout)
            typer.echo("Scan this QR code with WhatsApp (Settings > Linked Devices > Link a device)")
            return
        if method == "phone-code" and status.get("last_pairing_code"):
            typer.echo(f"Pairing code: {status['last_pairing_code']}")
            typer.echo("Enter this code on your phone (Settings > Linked Devices > Link with phone number)")
            return

    typer.echo("Timed out waiting for pairing info. Try 'cyborg whatsapp bridge-status' to check.")


@whatsapp_app.command("send")
def whatsapp_send(
    chat_id: Annotated[str, typer.Option("--chat-id", help="WhatsApp chat JID (e.g., 1234567890@s.whatsapp.net)")],
    text: Annotated[str, typer.Option("--text", help="Message text to send")],
    reply_to: Annotated[Optional[str], typer.Option("--reply-to", help="WhatsApp message ID to reply to")] = None,
) -> None:
    """Send a WhatsApp message."""
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    result = _api_call("POST", "/whatsapp/send", payload)
    _echo_json(result)


@whatsapp_app.command("bridge-status")
def whatsapp_bridge_status() -> None:
    """Show internal bridge status including queue sizes and uptime."""
    result = _api_call("GET", "/whatsapp/bridge-status")
    _echo_json(result)


app.add_typer(whatsapp_app, name="whatsapp")


def main() -> int:
    """CLI entry point for `python -m cyborg.cli`."""

    app()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
