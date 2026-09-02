"""Bob CLI webhook subapp."""

from __future__ import annotations

from bob_server.cli._helpers import *  # noqa: F403,F405


app = typer.Typer(help="Webhook operations")



@app.command("create")
def webhook_create(
    name: Annotated[str, typer.Argument(help="Webhook name")],
    url: Annotated[str, typer.Option("--url", help="Webhook target URL")],
    secret: Annotated[str, typer.Option("--secret", help="Webhook signing secret")],
    events: Annotated[Optional[list[str]], typer.Option("--event", help="Webhook event; repeat to add more")] = None,
    retry_count: Annotated[int, typer.Option("--retry-count", help="Webhook retry count")] = 3,
) -> None:
    """Create a webhook configuration."""

    if not events:
        raise typer.BadParameter("At least one --event value is required")
    webhook = _api_call(
        "POST",
        "/api/v1/webhooks",
        {"name": name, "url": url, "secret": secret, "events": events, "retry_count": retry_count},
    )["data"]
    typer.echo(f"Created webhook: {webhook['id']}")
    typer.echo(f"Name: {webhook['name']}")


@app.command("list")
def webhook_list(
    active_only: Annotated[bool, typer.Option("--active-only/--include-inactive", help="List only active webhooks")] = True,
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (table, json)")] = "table",
) -> None:
    """List webhook configurations."""

    webhooks = _api_call("GET", f"/api/v1/webhooks{_query_string(active_only=str(active_only).lower())}")["data"]
    if format == "json":
        _echo_json(webhooks)
        return
    if not webhooks:
        typer.echo("No webhooks found.")
        return
    typer.echo(f"{'ID':<36} {'Active':<8} {'Name'}")
    typer.echo("-" * 80)
    for webhook in webhooks:
        active = "yes" if webhook["is_active"] else "no"
        typer.echo(f"{webhook['id']:<36} {active:<8} {webhook['name']}")


@app.command("get")
def webhook_get(config_id: Annotated[str, typer.Argument(help="Webhook configuration ID")]) -> None:
    """Get a webhook configuration by ID."""

    _echo_json(_api_call("GET", f"/api/v1/webhooks/{config_id}")["data"])


@app.command("by-name")
def webhook_get_by_name(name: Annotated[str, typer.Argument(help="Webhook name")]) -> None:
    """Get a webhook configuration by name."""

    _echo_json(_api_call("GET", f"/api/v1/webhooks/by-name/{name}")["data"])


@app.command("update")
def webhook_update(
    config_id: Annotated[str, typer.Argument(help="Webhook configuration ID")],
    url: Annotated[Optional[str], typer.Option("--url", help="Webhook target URL")] = None,
    secret: Annotated[Optional[str], typer.Option("--secret", help="Webhook signing secret")] = None,
    events: Annotated[Optional[list[str]], typer.Option("--event", help="Webhook event; repeat to add more")] = None,
    retry_count: Annotated[Optional[int], typer.Option("--retry-count", help="Webhook retry count")] = None,
    is_active: Annotated[Optional[bool], typer.Option("--active/--inactive", help="Activate or deactivate the webhook")] = None,
) -> None:
    """Update a webhook configuration."""

    payload: dict[str, Any] = {}
    if url is not None:
        payload["url"] = url
    if secret is not None:
        payload["secret"] = secret
    if events:
        payload["events"] = events
    if retry_count is not None:
        payload["retry_count"] = retry_count
    if is_active is not None:
        payload["is_active"] = is_active
    webhook = _api_call("PUT", f"/api/v1/webhooks/{config_id}", payload)["data"]
    typer.echo(f"Updated webhook: {webhook['id']}")
    typer.echo(f"Name: {webhook['name']}")


@app.command("delete")
def webhook_delete(config_id: Annotated[str, typer.Argument(help="Webhook configuration ID")]) -> None:
    """Delete a webhook configuration."""

    _api_call("DELETE", f"/api/v1/webhooks/{config_id}")
    typer.echo(f"Webhook deleted: {config_id}")


@app.command("deliveries")
def webhook_deliveries(
    webhook_id: Annotated[Optional[str], typer.Option("--webhook-id", help="Filter by webhook ID")] = None,
    status: Annotated[Optional[str], typer.Option("--status", help="Filter by delivery status")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Maximum number of deliveries")] = 100,
    offset: Annotated[int, typer.Option("--offset", help="Result offset")] = 0,
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (table, json)")] = "table",
) -> None:
    """List webhook deliveries."""

    deliveries = _api_call(
        "GET",
        f"/api/v1/webhooks/deliveries{_query_string(webhook_id=webhook_id, status=status, limit=limit, offset=offset)}",
    )["data"]
    if format == "json":
        _echo_json(deliveries)
        return
    if not deliveries:
        typer.echo("No deliveries found.")
        return
    typer.echo(f"{'ID':<36} {'Status':<12} {'Event':<24} {'Attempt'}")
    typer.echo("-" * 100)
    for delivery in deliveries:
        event_name = str(delivery.get("event", ""))[:24]
        attempt = delivery.get("attempt_count", "")
        typer.echo(f"{delivery['id']:<36} {delivery.get('status', ''):<12} {event_name:<24} {attempt}")


@app.command("delivery-get")
def webhook_delivery_get(delivery_id: Annotated[str, typer.Argument(help="Delivery ID")]) -> None:
    """Get a webhook delivery by ID."""

    _echo_json(_api_call("GET", f"/api/v1/webhooks/deliveries/{delivery_id}")["data"])


@app.command("delivery-retry")
def webhook_delivery_retry(delivery_id: Annotated[str, typer.Argument(help="Delivery ID")]) -> None:
    """Retry a failed webhook delivery."""

    result = _api_call("POST", f"/api/v1/webhooks/deliveries/{delivery_id}/retry")["data"]
    typer.echo(f"Retried delivery: {delivery_id}")
    typer.echo(f"Success: {result['success']}")


@app.command("process-pending")
def webhook_process_pending() -> None:
    """Process pending webhook deliveries."""

    result = _api_call("POST", "/api/v1/webhooks/process-pending")["data"]
    typer.echo(f"Processed deliveries: {result['processed']}")



# ---------------------------------------------------------------------------
# Email relay
# ---------------------------------------------------------------------------
