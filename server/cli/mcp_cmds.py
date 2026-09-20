"""Bob CLI mcp subapp — manage MCP tool servers through the running
service's dashboard API (mutations there trigger manager reloads)."""

from __future__ import annotations

from server.cli._helpers import *  # noqa: F403,F405

app = typer.Typer(help="MCP tool servers: global or per-conversation")

_BASE = "/dashboard/api/mcp"


def _mcp_api(method: str, path: str, data: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """_api_call wraps every response in {"data": ...}; unwrap it so the
    documented endpoint shapes ({"servers": ...}, {"server": ...},
    {"error": ...}) reach the command bodies — including error keys, which
    the envelope otherwise hides from the "error" in result checks."""
    result = _api_call(method, path, data)
    return result.get("data", result)


def _resolve_server_id(server: str) -> str:
    """Accept a server id or name."""
    servers = _mcp_api("GET", f"{_BASE}/servers")["servers"]
    for row in servers:
        if row["id"] == server or row["name"] == server:
            return row["id"]
    typer.echo(f"No MCP server matching {server!r}", err=True)
    raise typer.Exit(code=1)


@app.command("list")
def list_servers() -> None:
    """List servers with cache status."""
    result = _mcp_api("GET", f"{_BASE}/servers")
    if not result["servers"]:
        typer.echo("No MCP servers registered.")
        return
    for s in result["servers"]:
        state = (f"{s['tool_count']} tools"
                 if not s.get("cache_error") else f"ERROR: {s['cache_error']}")
        flags = []
        if s["is_global"]:
            flags.append("global")
        if s["trusted_only"]:
            flags.append("trusted-only")
        if not s["enabled"]:
            flags.append("DISABLED")
        typer.echo(f"{s['name']}  [{s['transport']}]  {state}"
                   + (f"  ({', '.join(flags)})" if flags else ""))
        typer.echo(f"  id={s['id']}")


@app.command("add")
def add_server(
    name: str,
    transport: str = typer.Option(..., help="stdio or http"),
    command: Optional[str] = typer.Option(None, help="stdio: absolute path"),
    arg: list[str] = typer.Option([], "--arg", help="stdio arg (repeatable)"),
    env: list[str] = typer.Option([], "--env", help="KEY=VAL (repeatable)"),
    url: Optional[str] = typer.Option(None, help="http: server URL"),
    header: list[str] = typer.Option([], "--header", help="KEY:VAL (repeatable)"),
    is_global: bool = typer.Option(False, "--global", help="every conversation"),
    trusted_only: bool = typer.Option(False, "--trusted-only"),
    timeout: Optional[float] = typer.Option(None, help="call timeout seconds"),
    note: str = typer.Option(""),
) -> None:
    """Register a server (Mike-only surface; needs the dashboard secret)."""
    body: dict[str, Any] = {"name": name, "transport": transport,
                            "is_global": is_global,
                            "trusted_only": trusted_only, "note": note}
    if command:
        body["command"] = command
    if arg:
        body["args"] = arg
    if env:
        body["env"] = dict(e.split("=", 1) for e in env)
    if url:
        body["url"] = url
    if header:
        body["headers"] = dict(h.split(":", 1) for h in header)
    if timeout is not None:
        body["timeout_seconds"] = timeout
    result = _mcp_api("POST", f"{_BASE}/servers", body)
    if "error" in result:
        typer.echo(f"Error: {result['error']}", err=True)
        raise typer.Exit(code=1)
    _echo_json(result["server"])


@app.command("update")
def update_server(
    server: str,
    url: Optional[str] = typer.Option(None),
    timeout: Optional[float] = typer.Option(None),
    note: Optional[str] = typer.Option(None),
    is_global: Optional[bool] = typer.Option(None, "--global/--no-global"),
) -> None:
    """Update selected fields."""
    server_id = _resolve_server_id(server)
    body: dict[str, Any] = {}
    if url is not None:
        body["url"] = url
    if timeout is not None:
        body["timeout_seconds"] = timeout
    if note is not None:
        body["note"] = note
    if is_global is not None:
        body["is_global"] = is_global
    if not body:
        typer.echo("Nothing to update.", err=True)
        raise typer.Exit(code=1)
    result = _mcp_api("PUT", f"{_BASE}/servers/{server_id}", body)
    if "error" in result:
        typer.echo(f"Error: {result['error']}", err=True)
        raise typer.Exit(code=1)
    _echo_json(result["server"])


@app.command("remove")
def remove_server(server: str) -> None:
    """Delete a server and its conversation attachments."""
    server_id = _resolve_server_id(server)
    result = _mcp_api("DELETE", f"{_BASE}/servers/{server_id}")
    if "error" in result:
        typer.echo(f"Error: {result['error']}", err=True)
        raise typer.Exit(code=1)
    typer.echo("Removed.")


@app.command("enable")
def enable_server(server: str, enabled: bool = typer.Option(True, "--enabled/--disabled")) -> None:
    """Toggle a server without deleting it."""
    server_id = _resolve_server_id(server)
    _mcp_api("POST", f"{_BASE}/servers/{server_id}/enabled",
              {"enabled": enabled})
    typer.echo("Enabled." if enabled else "Disabled.")


@app.command("test")
def test_server(server: str) -> None:
    """Fresh-connection health check + tool listing."""
    server_id = _resolve_server_id(server)
    result = _mcp_api("POST", f"{_BASE}/servers/{server_id}/test")
    if result.get("ok"):
        typer.echo(f"OK — {len(result['tools'])} tools:")
        for name in result["tools"]:
            typer.echo(f"  {name}")
    else:
        typer.echo(f"FAILED: {result.get('error')}", err=True)
        raise typer.Exit(code=1)


@app.command("attach")
def attach(
    conversation: str = typer.Option(..., "--to", help="conversation id"),
    server: list[str] = typer.Option(..., "--server", help="server id/name (repeatable)"),
) -> None:
    """Attach servers to a conversation (replaces current attachments)."""
    ids = [_resolve_server_id(s) for s in server]
    result = _mcp_api(
        "PUT", f"/dashboard/api/conversations/{conversation}/mcp",
        {"server_ids": ids})
    if "error" in result:
        typer.echo(f"Error: {result['error']}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Attached {len(ids)} server(s) to {conversation}.")


@app.command("detach")
def detach(conversation: str = typer.Option(..., "--from")) -> None:
    """Remove all MCP attachments from a conversation."""
    result = _mcp_api(
        "PUT", f"/dashboard/api/conversations/{conversation}/mcp",
        {"server_ids": []})
    if "error" in result:
        typer.echo(f"Error: {result['error']}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Detached all MCP servers from {conversation}.")


@app.command("tools")
def tools(server: str) -> None:
    """List the tool names a server currently exposes (from cache)."""
    server_id = _resolve_server_id(server)
    result = _mcp_api("GET", f"{_BASE}/servers/{server_id}")
    row = result["server"]
    if row.get("cache_error"):
        typer.echo(f"Cache error: {row['cache_error']}", err=True)
    if not row.get("tools"):
        typer.echo("(no tools cached — try: bob mcp test SERVER)")
        return
    for tool in row["tools"]:
        typer.echo(f"mcp_{_slug(row['name'])}_{tool['name']}: "
                   f"{(tool.get('description') or '')[:80]}")


def _slug(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9_-]", "", name.strip().lower()) or "server"
