"""Typer CLI for running and managing Cyborg."""

from __future__ import annotations

import shutil
import shlex
import subprocess
from pathlib import Path
from typing import Annotated
from urllib.error import URLError
from urllib.request import urlopen

import typer
import uvicorn

from cyborg.config import DEFAULT_HOST, DEFAULT_PORT, Settings
from cyborg.main import create_app


SERVICE_NAME = "cyborg.service"
app = typer.Typer(help="Manage the Cyborg data service.")


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

[Install]
WantedBy=default.target
"""


def _health_status(settings: Settings) -> str:
    try:
        with urlopen(f"http://{settings.host}:{settings.port}/health", timeout=2) as response:
            return response.read().decode("utf-8")
    except URLError as exc:
        return f"unreachable ({exc.reason})"


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

    settings = Settings(host=host, port=port, data_dir=data_dir, config_dir=config_dir, db_path=db_path, log_level=log_level)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level=settings.log_level)


def main() -> int:
    """CLI entry point for `python -m cyborg.cli`."""

    app()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
