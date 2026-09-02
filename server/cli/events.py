"""Bob CLI event subapp."""

from __future__ import annotations

from bob_server.cli._helpers import *  # noqa: F403,F405


app = typer.Typer(help="Event operations")



@app.command("create")
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


@app.command("list")
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


@app.command("get")
def event_get(event_id: Annotated[str, typer.Argument(help="Event ID")]) -> None:
    """Get event details."""

    _echo_json(_api_call("GET", f"/api/v1/events/{event_id}")["data"])


@app.command("update")
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


@app.command("delete")
def event_delete(event_id: Annotated[str, typer.Argument(help="Event ID")]) -> None:
    """Delete an event."""

    _api_call("DELETE", f"/api/v1/events/{event_id}")
    typer.echo(f"Event deleted: {event_id}")


@app.command("confirm")
def event_confirm(event_id: Annotated[str, typer.Argument(help="Event ID")]) -> None:
    """Confirm an event."""

    event = _api_call("POST", f"/api/v1/events/{event_id}/confirm")["data"]
    typer.echo(f"Event confirmed: {event['title']}")


@app.command("cancel")
def event_cancel(event_id: Annotated[str, typer.Argument(help="Event ID")]) -> None:
    """Cancel an event."""

    event = _api_call("POST", f"/api/v1/events/{event_id}/cancel")["data"]
    typer.echo(f"Event cancelled: {event['title']}")


@app.command("recipients")
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


@app.command("recipient-add")
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


@app.command("recipient-update")
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
