"""Bob CLI phone/call subapp."""

from __future__ import annotations

from bob_server.cli._helpers import *  # noqa: F403,F405


app = typer.Typer(help="Phone call operations")



@app.command("call")
def phone_call(
    to: Annotated[str, typer.Argument(help="Phone number to call (E.164 format, e.g. +1234567890)")],
    agenda: Annotated[str, typer.Option("--agenda", help="Purpose and handling instructions for the call")],
) -> None:
    """Initiate an outbound phone call with an agenda for the voice assistant."""
    result = _api_call("POST", "/phone/call", {"to": to, "agenda": agenda})
    _echo_json(result.get("data", result))


@app.command("list")
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


@app.command("status")
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

