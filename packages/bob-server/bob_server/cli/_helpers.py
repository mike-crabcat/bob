"""Shared helpers for the Bob CLI.

All private helpers and module-level constants used across CLI subapps live
here. Subapp modules import via ``from bob_server.cli._helpers import *``.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import typer
import uvicorn

from bob_server.config import DEFAULT_HOST, DEFAULT_PORT, Settings
from bob_server.main import create_app


SERVICE_NAME = "bob.service"
WHATSAPP_SERVICE_NAME = "whatsappbridge.service"


__all__ = [
    # Constants
    "SERVICE_NAME", "WHATSAPP_SERVICE_NAME",
    # Settings type re-export (used by some subapps)
    "Settings", "DEFAULT_HOST", "DEFAULT_PORT",
    # typer + uvicorn (so subapps don't repeat the import; tests patch cli.uvicorn)
    "typer", "uvicorn",
    # main.create_app (tests patch cli.create_app)
    "create_app",
    # typing helpers
    "Annotated", "Any", "Optional",
    # stdlib re-exports used by command bodies and tests (cli.urlopen)
    "json", "Path", "datetime", "timedelta",
    "urlopen", "Request", "HTTPError", "URLError", "urlencode", "quote",
    # Service management helpers
    "_service_file_path", "_run_command", "_systemctl",
    "_service_file_contents",
    "_whatsapp_service_file_path", "_whatsapp_service_file_contents",
    "_bridge_source_dir", "_bridge_token_in_env_file",
    "_health_status",
    # API helpers
    "_normalize_api_response", "_handle_http_error", "_handle_connection_error",
    "_api_call", "_text_call", "_echo_json", "_query_string",
    # Domain helpers
    "_resolve_inbox_id", "_parse_json_option", "_build_metadata",
    "_parse_time_expression", "_time_to_iso", "_resolve_calendar_id",
    "_build_contact_payload", "_build_session_route_payload",
    "_print_contact_table", "_print_session_route_table", "_print_event_table",
]


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
            "bob",
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
Description=Bob Data Service
After=default.target

[Service]
Type=simple
WorkingDirectory={working_dir}
ExecStart={quoted}
Restart=on-failure
Environment=PYTHONUNBUFFERED=1
Environment=BOB_CONFIG_DIR={settings.config_dir}

[Install]
WantedBy=default.target
"""


def _whatsapp_service_file_path() -> Path:
    return Path.home() / ".config/systemd/user" / WHATSAPP_SERVICE_NAME


def _whatsapp_service_file_contents(binary_path: Path, config_dir: Path) -> str:
    return f"""[Unit]
Description=Bob WhatsApp Bridge
After=default.target
Wants={SERVICE_NAME}

[Service]
Type=simple
ExecStart={binary_path}
Restart=on-failure
Environment=BOB_CONFIG_DIR={config_dir}
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""


def _bridge_source_dir() -> Path:
    """Locate the repo's services/whatsappbridge directory.

    Checks CWD first (so `bob whatsapp service install` works from the repo root),
    then walks up from this file's location (so it works from an installed `bob`
    CLI as long as the source tree is still present on disk).
    """

    candidates: list[Path] = [Path.cwd()]
    here = Path(__file__).resolve()
    candidates.extend(here.parents)
    for base in candidates:
        candidate = base / "services" / "whatsappbridge"
        if (candidate / "Makefile").is_file():
            return candidate
    raise typer.BadParameter(
        "Could not locate services/whatsappbridge/. Run `bob whatsapp service install` "
        "from the repo root, or ensure the source tree is present."
    )


_TOKEN_LINE_PATTERN = re.compile(
    r"^\s*(?:export\s+)?BOB_WHATSAPP_BRIDGE_TOKEN\s*=\s*(?P<value>.*?)\s*$"
)


def _bridge_token_in_env_file(config_dir: Path) -> str | None:
    """Read BOB_WHATSAPP_BRIDGE_TOKEN from <config_dir>/.env directly.

    Reads the file the bridge will actually read under systemd, bypassing
    os.environ (which may have been polluted by import-time .env loading from
    a different config dir). Returns the unquoted value or None if absent.
    """

    env_path = config_dir / ".env"
    if not env_path.is_file():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = _TOKEN_LINE_PATTERN.match(line)
        if not match:
            continue
        value = match.group("value")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        return value
    return None


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
    typer.echo("Is the bob service running? Try: bob start", err=True)
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
        raise typer.BadParameter("No active email inboxes found. Register one with `bob email-inbox register`.")
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
