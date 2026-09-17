"""Bob CLI steer subapp — wake one of Bob's conversations at the owner's
request (the reusable operator lever for "get Bob to message X about Y").

The target turn composes and sends its own message in its own voice; the
instruction is intent, never text to parrot. Delivery runs inside the live
service (dashboard API → wake_conversation → connected WhatsApp bridge), so
the CLI process never touches WhatsApp itself.
"""

from __future__ import annotations

from server.cli._helpers import *  # noqa: F403,F405

app = typer.Typer(help="Steer a conversation at the owner's request",
                  invoke_without_command=True)


@app.callback()
def steer(
    target: str = typer.Argument(
        ..., help="Phone number (DM), group id, group name, or raw session key"),
    instruction: str = typer.Option(
        ..., "--instruction", "-i",
        help="Self-contained intent — the target conversation acts on this "
             "and writes its own message"),
) -> None:
    """Wake TARGET with INSTRUCTION; it replies in its own voice."""
    result = _api_call(
        "POST", "/dashboard/api/conversations/wake",
        {"target": target, "instruction": instruction})
    # _api_call normalizes every body into {"data": ...}
    payload = result.get("data") if isinstance(result.get("data"), dict) else result
    if payload.get("error"):
        typer.echo(f"Error: {payload['error']}", err=True)
        for cand in payload.get("candidates") or []:
            typer.echo(f"  candidate: {cand.get('name', '?')}", err=True)
        raise typer.Exit(code=1)
    state = "dispatched" if payload.get("dispatched") else (
        "stored (no dispatcher available — recovers on next inbound/restart)")
    typer.echo(f"Steered {payload.get('session_key')} — {state}.")
