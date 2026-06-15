"""Bob CLI top-level service management commands.

These are top-level commands (``bob install``, ``bob serve``, etc.) — not a
subapp. Functions are defined at module level (so tests and other callers can
import them) and registered onto the main ``app`` via :func:`register`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Annotated

from bob_server.cli._helpers import *  # noqa: F403,F405


def install(
    host: Annotated[str, typer.Option(help="Host address for the service")] = DEFAULT_HOST,
    port: Annotated[int, typer.Option(help="TCP port for the service")] = DEFAULT_PORT,
    data_dir: Annotated[Path, typer.Option(help="Directory for the SQLite database")] = Path("~/data"),
    config_dir: Annotated[Path, typer.Option(help="Directory for Bob config")] = Path("~/config"),
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


def start() -> None:
    """Start the systemd user service."""

    _systemctl("start", SERVICE_NAME)
    typer.echo("Service started")


def stop() -> None:
    """Stop the systemd user service."""

    _systemctl("stop", SERVICE_NAME)
    typer.echo("Service stopped")


def restart() -> None:
    """Restart the systemd user service."""

    _systemctl("restart", SERVICE_NAME)
    typer.echo("Service restarted")


def status() -> None:
    """Show systemd state and the HTTP health endpoint."""

    settings = Settings.from_env()
    result = _systemctl("status", "--no-pager", SERVICE_NAME)
    typer.echo(result.stdout)
    typer.echo(f"Health: {_health_status(settings)}")


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


def serve(
    host: Annotated[str, typer.Option(help="Host address to bind")] = DEFAULT_HOST,
    port: Annotated[int, typer.Option(help="TCP port to bind")] = DEFAULT_PORT,
    data_dir: Annotated[Path, typer.Option(help="Directory for the SQLite database")] = Path("~/data"),
    config_dir: Annotated[Path, typer.Option(help="Directory for config files")] = Path("~/config"),
    db_path: Annotated[Path | None, typer.Option(help="Override SQLite database path")] = None,
    log_level: Annotated[str, typer.Option(help="Uvicorn log level")] = "info",
) -> None:
    """Run the API server directly."""

    previous_config_dir = os.environ.get("BOB_CONFIG_DIR")
    os.environ["BOB_CONFIG_DIR"] = str(config_dir.expanduser())
    try:
        env_settings = Settings.from_env()
    finally:
        if previous_config_dir is None:
            os.environ.pop("BOB_CONFIG_DIR", None)
        else:
            os.environ["BOB_CONFIG_DIR"] = previous_config_dir

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


_COMMANDS = [install, uninstall, start, stop, restart, status, logs, serve]


def register(app: typer.Typer) -> None:
    """Register all top-level service commands onto ``app``."""
    for func in _COMMANDS:
        app.command()(func)


__all__ = [func.__name__ for func in _COMMANDS] + ["register"]
