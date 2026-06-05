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
app = typer.Typer(help="Cyborg - Bob's memory and communication service.")

contact_app = typer.Typer(help="Contact operations")
memory_app = typer.Typer(help="Memory wiki operations")
session_route_app = typer.Typer(help="Session route registry operations")
calendar_app = typer.Typer(help="Calendar operations")
event_app = typer.Typer(help="Event operations")
context_app = typer.Typer(help="Context operations")
webhook_app = typer.Typer(help="Webhook operations")
phone_app = typer.Typer(help="Phone call operations")
openai_app = typer.Typer(help="OpenAI LLM evaluation commands")
eval_app = typer.Typer(help="LLM eval framework")

app.add_typer(contact_app, name="contact")
app.add_typer(memory_app, name="memory")
app.add_typer(session_route_app, name="session-route")
app.add_typer(calendar_app, name="calendar")
app.add_typer(event_app, name="event")
app.add_typer(context_app, name="context")
app.add_typer(webhook_app, name="webhook")
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



def _build_contact_payload(
    *,
    name: Optional[str] = None,
    phone_number: Optional[str] = None,
    email: Optional[str] = None,
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



def _print_contact_table(contacts: list[dict[str, Any]]) -> None:
    typer.echo(f"{'ID':<36} {'Phone':<18} {'Name'}")
    typer.echo("-" * 90)
    for contact in contacts:
        typer.echo(f"{contact['id']:<36} {contact['phone_number']:<18} {contact['name'][:32]}")



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
        agentmail=env_settings.agentmail,
        email_polling_enabled=env_settings.email_polling_enabled,
        heartbeat_interval_seconds=env_settings.heartbeat_interval_seconds,
        public_url=env_settings.public_url,
        dashboard_secret=env_settings.dashboard_secret,
        voice=env_settings.voice,
        phone=env_settings.phone,
        openai=env_settings.openai,
        harness=env_settings.harness,
        whatsapp_bridge=env_settings.whatsapp_bridge,
    )
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level=settings.log_level)




@contact_app.command("create")
def contact_create(
    name: Annotated[str, typer.Argument(help="Contact name")],
    phone_number: Annotated[str, typer.Option("--phone-number", "--phone", "-p", help="Contact phone number")] = ...,
    email: Annotated[Optional[str], typer.Option("--email", "-e", help="Contact email")] = None,
    metadata_json: Annotated[Optional[str], typer.Option("--metadata-json", help="Contact metadata as JSON object")] = None,
    channel: Annotated[Optional[str], typer.Option(help="Messaging channel for routing")] = None,
    chat_id: Annotated[Optional[str], typer.Option(help="Chat or room identifier for routing")] = None,
    session_key: Annotated[Optional[str], typer.Option(help="Session key for routing")] = None,
) -> None:
    """Create a contact."""

    payload = _build_contact_payload(
        name=name,
        phone_number=phone_number,
        email=email,
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
    if contact.get("metadata"):
        typer.echo(f"Metadata: {json.dumps(contact['metadata'])}")


@contact_app.command("update")
def contact_update(
    contact_id: Annotated[str, typer.Argument(help="Contact ID")],
    name: Annotated[Optional[str], typer.Option(help="Contact name")] = None,
    phone_number: Annotated[Optional[str], typer.Option("--phone-number", "--phone", "-p", help="Contact phone number")] = None,
    email: Annotated[Optional[str], typer.Option("--email", "-e", help="Contact email")] = None,
    metadata_json: Annotated[Optional[str], typer.Option("--metadata-json", help="Contact metadata as JSON object")] = None,
    channel: Annotated[Optional[str], typer.Option(help="Messaging channel for routing")] = None,
    chat_id: Annotated[Optional[str], typer.Option(help="Chat or room identifier for routing")] = None,
    session_key: Annotated[Optional[str], typer.Option(help="Session key for routing")] = None,
) -> None:
    """Update a contact."""

    payload = _build_contact_payload(
        name=name,
        phone_number=phone_number,
        email=email,
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



@calendar_app.command("create")
def calendar_create(
    name: Annotated[str, typer.Argument(help="Calendar name")],
    description: Annotated[Optional[str], typer.Option("--description", "-d", help="Calendar description")] = None,
    color: Annotated[Optional[str], typer.Option("--color", "-c", help="Calendar color (#RRGGBB)")] = None,
    is_default: Annotated[bool, typer.Option("--default", help="Set as default calendar")] = False,
    metadata_json: Annotated[Optional[str], typer.Option("--metadata-json", help="Calendar metadata as JSON object")] = None,
    channel: Annotated[Optional[str], typer.Option(help="Messaging channel for routing")] = None,
    chat_id: Annotated[Optional[str], typer.Option(help="Chat or room identifier for routing")] = None,
    session_key: Annotated[Optional[str], typer.Option(help="Session key for reminder routing")] = None,
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
    session_key: Annotated[Optional[str], typer.Option(help="Session key for reminder routing")] = None,
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
    typer.echo("CONTEXT SUMMARY")
    typer.echo("=" * 60)
    typer.echo(f"Generated: {ctx['generated_at']}")
    typer.echo()

    if ctx["upcoming_events"]:
        typer.echo("UPCOMING EVENTS:")
        for event in ctx["upcoming_events"][:5]:
            start = event["start_time"][:16].replace("T", " ")
            typer.echo(f"  * {start} - {event['title']}")


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
            typer.echo(f"  Latency:   STT {ex.get('stt_ms', '—')}ms | LLM {ex.get('llm_total_ms', '—')}ms | TTFP {ex.get('tts_first_chunk_ms', '—')}ms | E2E {e2e}ms")
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


# ── Memory commands ─────────────────────────────────────────────


@memory_app.command("seed")
def memory_seed(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be processed without calling LLM")] = False,
) -> None:
    """Regenerate memory from all session history using the bulletin generator."""
    import asyncio
    asyncio.run(_memory_seed(dry_run))


async def _memory_seed(dry_run: bool) -> None:
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
        from cyborg_server.services.memory.seed import seed_from_history

        workspace = settings.harness.workspace_dir
        result = await seed_from_history(ctx, workspace, dry_run=dry_run)

        typer.echo(f"\nSeed result:")
        typer.echo(f"  Sessions processed: {result.get('sessions_processed', 0)}")
        typer.echo(f"  Bulletins generated: {result.get('bulletins_generated', 0)}")
        typer.echo(f"  Bulletins skipped: {result.get('bulletins_skipped', 0)}")
        typer.echo(f"  Errors: {len(result.get('errors', []))}")
    finally:
        await db.close()


@memory_app.command("seed-email")
def memory_seed_email(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be processed without calling LLM")] = False,
    thread_id: Annotated[Optional[str], typer.Option("--thread", help="Process a specific email thread by agentmail_thread_id")] = None,
) -> None:
    """Regenerate memory from email thread history using the bulletin generator."""
    import asyncio
    asyncio.run(_memory_seed_email(dry_run, thread_id))


async def _memory_seed_email(dry_run: bool, thread_id: str | None) -> None:
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
        from cyborg_server.services.memory.seed_email import seed_from_email_history

        workspace = settings.harness.workspace_dir
        result = await seed_from_email_history(ctx, workspace, dry_run=dry_run, thread_id=thread_id)

        typer.echo(f"\nSeed-email result:")
        typer.echo(f"  Threads processed: {result.get('threads_processed', 0)}")
        typer.echo(f"  Bulletins generated: {result.get('bulletins_generated', 0)}")
        typer.echo(f"  Bulletins skipped: {result.get('bulletins_skipped', 0)}")
        typer.echo(f"  Errors: {len(result.get('errors', []))}")
    finally:
        await db.close()


@memory_app.command("seed-manual")
def memory_seed_manual(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be processed without writing")] = False,
) -> None:
    """Replay memory_write tool calls from LLM logs as bulletins."""
    import asyncio
    asyncio.run(_memory_seed_manual(dry_run))


async def _memory_seed_manual(dry_run: bool) -> None:
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
        from cyborg_server.services.memory.seed_manual import seed_manual_bulletins

        workspace = settings.harness.workspace_dir
        result = await seed_manual_bulletins(ctx, workspace, dry_run=dry_run)

        typer.echo(f"\nSeed-manual result:")
        typer.echo(f"  Log rows scanned: {result.get('log_rows_scanned', 0)}")
        typer.echo(f"  Bulletins generated: {result.get('bulletins_generated', 0)}")
        typer.echo(f"  Errors: {len(result.get('errors', []))}")
    finally:
        await db.close()


@memory_app.command("rebuild")
def memory_rebuild(
    all: Annotated[bool, typer.Option("--all", help="Rebuild all derived data from bulletins")] = False,
    entity_id: Annotated[Optional[str], typer.Option("--entity", help="Rebuild indexes for a specific entity")] = None,
) -> None:
    """Rebuild memory indexes and derived data from bulletins."""
    import asyncio
    asyncio.run(_memory_rebuild(all, entity_id))


async def _memory_rebuild(all: bool, entity_id: str | None) -> None:
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
        from cyborg_server.services.memory import MemoryService

        workspace = settings.harness.workspace_dir
        svc = MemoryService(ctx)

        result = await svc.rebuild(workspace, entity_id=entity_id, all=all)
        typer.echo(f"Rebuild result: {json.dumps(result, indent=2)}")
    finally:
        await db.close()


@memory_app.command("validate")
def memory_validate() -> None:
    """Validate memory structure: check frontmatter, dangling refs, required fields."""
    import asyncio
    asyncio.run(_memory_validate())


async def _memory_validate() -> None:
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
        from cyborg_server.services.memory import MemoryService

        workspace = settings.harness.workspace_dir
        svc = MemoryService(ctx)

        result = await svc.validate(workspace)
        if result["valid"]:
            typer.echo("Memory is valid.")
        else:
            typer.echo(f"Issues found ({len(result['issues'])}):")
            for issue in result["issues"]:
                typer.echo(f"  - {issue}")
    finally:
        await db.close()


@memory_app.command("cleanup-contacts")
def memory_cleanup_contacts(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would change without writing")] = False,
) -> None:
    """Remove duplicate contact entities and rewire references to canonical IDs."""
    import asyncio
    asyncio.run(_memory_cleanup_contacts(dry_run))


async def _memory_cleanup_contacts(dry_run: bool) -> None:
    from cyborg_server.config import Settings
    from cyborg_server.context import AppContext
    from cyborg_server.database import Database
    from cyborg_server.services.memory.cleanup import run_cleanup, build_renaming_map
    from cyborg_server.services.memory.contact_directory import ContactDirectory

    settings = Settings.from_env()
    schema_dir = Path(__file__).parent / "schemas"
    db_path = settings.db_path or Path("cyborg.db")
    db = Database(db_path, schema_dir)
    await db.connect()

    try:
        workspace = settings.harness.workspace_dir
        memory_dir = workspace / "memory"
        directory = await ContactDirectory.load(db)

        typer.echo(f"Loaded {len(directory.all_canonical_ids())} contacts from DB")

        if dry_run:
            rename, merge = await build_renaming_map(db, directory)
            typer.echo(f"\n[Dry run] Would rename {len(rename)} entities")
            typer.echo(f"[Dry run] Would merge {len(merge)} duplicates into canonical entities")
            for old, new in sorted(rename.items()):
                typer.echo(f"  {old} -> {new}")
            return

        result = await run_cleanup(db, directory, dry_run=False)
        typer.echo("\nCleanup result:")
        typer.echo(f"  Renamed: {result['renamed']}")
        typer.echo(f"  Merged:  {result['merged']}")
        typer.echo(f"  Deleted: {result['deleted']}")
        typer.echo(f"  Rewritten claims:     {result['rewritten_claims']}")
        typer.echo(f"  Rewritten bulletins:  {result['rewritten_bulletins']}")
        typer.echo(f"  Rewritten related:    {result['rewritten_related']}")
        typer.echo(f"  Enriched with DB FK:  {result['enriched']}")
    finally:
        await db.close()


@memory_app.command("query")
def memory_query(
    question: Annotated[str, typer.Argument(help="Question to search memory for")],
    entity_type: Annotated[str, typer.Option("--type", help="Filter to entity type")] = "",
    actor: Annotated[Optional[str], typer.Option("--actor", help="Actor contact ID")] = None,
    channel: Annotated[Optional[str], typer.Option("--channel", help="Channel context")] = None,
) -> None:
    """Query memory with a natural language question."""
    import asyncio
    asyncio.run(_memory_query(question, entity_type, actor, channel))


async def _memory_query(question: str, entity_type: str, actor: str | None, channel: str | None) -> None:
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
        from cyborg_server.services.memory import MemoryService

        workspace = settings.harness.workspace_dir
        svc = MemoryService(ctx)

        result = await svc.search_entries(workspace, question, entity_type=entity_type or "")
        typer.echo(f"\nAbstract: {result.get('abstract', '')}")
        typer.echo(f"\nResults ({len(result.get('results', []))}):")
        for r in result.get("results", []):
            typer.echo(f"  - {r.get('entity_id', '')} ({r.get('entity_type', '')})")
            if r.get("relevance"):
                typer.echo(f"    {r['relevance']}")
    finally:
        await db.close()


def main() -> int:
    """CLI entry point for `python -m cyborg.cli`."""

    app()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
