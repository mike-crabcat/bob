"""Bob CLI whatsapp subapp."""

from __future__ import annotations

from bob_server.cli._helpers import *  # noqa: F403,F405

# ============================================================================
# WhatsApp commands
# ============================================================================

app = typer.Typer(help="WhatsApp bridge operations")


@app.command("status")
def whatsapp_status() -> None:
    """Show WhatsApp bridge connection status."""
    result = _api_call("GET", "/whatsapp/status")
    _echo_json(result)


@app.command("pair")
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

    typer.echo("Timed out waiting for pairing info. Try 'bob whatsapp bridge-status' to check.")


@app.command("send")
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


@app.command("bridge-status")
def whatsapp_bridge_status() -> None:
    """Show internal bridge status including queue sizes and uptime."""
    result = _api_call("GET", "/whatsapp/bridge-status")
    _echo_json(result)


# ── WhatsApp bridge service lifecycle ─────────────────────────────

service_app = typer.Typer(help="WhatsApp bridge systemd service lifecycle")


def _bridge_reachable(settings: Settings) -> str:
    """TCP-probe the bridge listen address from settings.whatsapp_bridge.url.

    Returns 'reachable' or 'unreachable (<reason>)'. Uses a raw TCP connect so
    the result reflects only the bridge process, not bob-server's WebSocket
    client or HTTP proxy.
    """

    url = settings.whatsapp_bridge.url
    # url looks like ws://127.0.0.1:8430/ws
    if "://" in url:
        url = url.split("://", 1)[1]
    host_port = url.split("/", 1)[0]
    if ":" not in host_port:
        return "unreachable (no port in bridge url)"
    host, port_str = host_port.rsplit(":", 1)
    try:
        port = int(port_str)
    except ValueError:
        return f"unreachable (bad port: {port_str})"
    try:
        with socket.create_connection((host, port), timeout=2):
            return "reachable"
    except OSError as exc:
        return f"unreachable ({exc})"


@service_app.command("install")
def whatsapp_service_install(
    config_dir: Annotated[Path, typer.Option(help="Config directory where .env lives")] = Path("~/config"),
) -> None:
    """Build the bridge, write the systemd unit, enable and start it."""

    resolved_config_dir = config_dir.expanduser()
    if not _bridge_token_in_env_file(resolved_config_dir):
        typer.echo(
            f"Error: BOB_WHATSAPP_BRIDGE_TOKEN is not set in {resolved_config_dir / '.env'}. "
            "Add it and re-run `bob whatsapp service install`.",
            err=True,
        )
        raise typer.Exit(code=1)

    source_dir = _bridge_source_dir()
    binary_path = (source_dir / "bin" / "whatsappbridge").resolve()

    _run_command(["make", "-C", str(source_dir), "build"])
    if not binary_path.exists():
        typer.echo(f"Error: build did not produce binary at {binary_path}", err=True)
        raise typer.Exit(code=1)

    service_path = _whatsapp_service_file_path()
    service_path.parent.mkdir(parents=True, exist_ok=True)
    service_path.write_text(
        _whatsapp_service_file_contents(binary_path, resolved_config_dir),
        encoding="utf-8",
    )
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", WHATSAPP_SERVICE_NAME)

    settings = Settings.from_env()
    typer.echo(f"Installed {WHATSAPP_SERVICE_NAME}")
    typer.echo(f"  Unit:   {service_path}")
    typer.echo(f"  Binary: {binary_path}")
    typer.echo(f"  Bridge: {settings.whatsapp_bridge.url}")


@service_app.command("uninstall")
def whatsapp_service_uninstall() -> None:
    """Disable and remove the systemd unit. Keeps the binary and data."""

    service_path = _whatsapp_service_file_path()
    if service_path.exists():
        try:
            _systemctl("disable", "--now", WHATSAPP_SERVICE_NAME)
        except typer.Exit:
            pass
        service_path.unlink()
        _systemctl("daemon-reload")
        _systemctl("reset-failed", WHATSAPP_SERVICE_NAME)
        typer.echo(f"Removed {service_path}")
    else:
        typer.echo("Service file is not installed")


@service_app.command("start")
def whatsapp_service_start() -> None:
    """Start the bridge systemd user service."""

    _systemctl("start", WHATSAPP_SERVICE_NAME)
    typer.echo("Bridge service started")


@service_app.command("stop")
def whatsapp_service_stop() -> None:
    """Stop the bridge systemd user service."""

    _systemctl("stop", WHATSAPP_SERVICE_NAME)
    typer.echo("Bridge service stopped")


@service_app.command("restart")
def whatsapp_service_restart() -> None:
    """Restart the bridge systemd user service."""

    _systemctl("restart", WHATSAPP_SERVICE_NAME)
    typer.echo("Bridge service restarted")


@service_app.command("status")
def whatsapp_service_status() -> None:
    """Show systemd state and bridge reachability."""

    settings = Settings.from_env()
    result = _systemctl("status", "--no-pager", WHATSAPP_SERVICE_NAME)
    typer.echo(result.stdout)
    typer.echo(f"Bridge: {_bridge_reachable(settings)}")


@service_app.command("logs")
def whatsapp_service_logs(
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Follow logs")] = False,
    lines: Annotated[int, typer.Option("--lines", "-n", help="Number of lines to show")] = 200,
) -> None:
    """Print journalctl logs for the bridge service."""

    command = ["journalctl", "--user", "-u", WHATSAPP_SERVICE_NAME, "--no-pager", "-n", str(lines)]
    if follow:
        command.append("-f")
        subprocess.run(command, check=False)
        return
    result = _run_command(command)
    typer.echo(result.stdout)


app.add_typer(service_app, name="service")


# ── Memory commands ─────────────────────────────────────────────

