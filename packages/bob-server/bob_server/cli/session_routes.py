"""Bob CLI session-route subapp."""

from __future__ import annotations

from bob_server.cli._helpers import *  # noqa: F403,F405


app = typer.Typer(help="Session route registry operations")



@app.command("create")
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


@app.command("list")
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


@app.command("get")
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


@app.command("update")
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


@app.command("delete")
def session_route_delete(route_id: Annotated[str, typer.Argument(help="Session route ID")]) -> None:
    """Delete a session route."""

    _api_call("DELETE", f"/api/v1/session-routes/{route_id}")
    typer.echo(f"Session route deleted: {route_id}")

