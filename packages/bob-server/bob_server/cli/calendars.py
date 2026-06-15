"""Bob CLI calendar subapp."""

from __future__ import annotations

from bob_server.cli._helpers import *  # noqa: F403,F405


app = typer.Typer(help="Calendar operations")



@app.command("create")
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


@app.command("list")
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


@app.command("get")
def calendar_get(calendar_id: Annotated[str, typer.Argument(help="Calendar ID")]) -> None:
    """Get calendar details."""

    _echo_json(_api_call("GET", f"/api/v1/calendars/{calendar_id}")["data"])


@app.command("update")
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


@app.command("delete")
def calendar_delete(calendar_id: Annotated[str, typer.Argument(help="Calendar ID")]) -> None:
    """Delete a calendar."""

    _api_call("DELETE", f"/api/v1/calendars/{calendar_id}")
    typer.echo(f"Calendar deleted: {calendar_id}")

