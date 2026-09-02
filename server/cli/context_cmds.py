"""Bob CLI context subapp."""

from __future__ import annotations

from bob_server.cli._helpers import *  # noqa: F403,F405


app = typer.Typer(help="Context operations")



@app.command("summary")
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


@app.command("calendar")
def context_calendar() -> None:
    """Get calendar-focused context."""

    _echo_json(_api_call("GET", "/api/v1/context/calendar")["data"])

