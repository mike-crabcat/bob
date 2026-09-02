"""Bob CLI contact subapp."""

from __future__ import annotations

from bob_server.cli._helpers import *  # noqa: F403,F405


app = typer.Typer(help="Contact operations")



@app.command("create")
def contact_create(
    name: Annotated[str, typer.Argument(help="Contact name")],
    phone_number: Annotated[str, typer.Option("--phone-number", "--phone", "-p", help="Contact phone number")] = ...,
    email: Annotated[Optional[str], typer.Option("--email", "-e", help="Contact email")] = None,
    metadata_json: Annotated[Optional[str], typer.Option("--metadata-json", help="Contact metadata as JSON object")] = None,
    channel: Annotated[Optional[str], typer.Option(help="Messaging channel for routing")] = None,
    chat_id: Annotated[Optional[str], typer.Option(help="Chat or room identifier for routing")] = None,
    session_key: Annotated[Optional[str], typer.Option(help="Session key for routing")] = None,
) -> None:
    """Create a contact."""

    payload = _build_contact_payload(
        name=name,
        phone_number=phone_number,
        email=email,
        metadata_json=metadata_json,
        channel=channel,
        chat_id=chat_id,
        session_key=session_key,
    )
    contact = _api_call("POST", "/api/v1/contacts", payload)["data"]
    typer.echo(f"Created contact: {contact['id']}")
    typer.echo(f"Name: {contact['name']}")
    typer.echo(f"Phone: {contact['phone_number']}")


@app.command("list")
def contact_list(
    search: Annotated[Optional[str], typer.Option("--search", "-s", help="Search by name, phone, or email")] = None,
    skip: Annotated[int, typer.Option("--skip", help="Pagination offset")] = 0,
    limit: Annotated[int, typer.Option("--limit", help="Page size")] = 100,
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (table, json)")] = "table",
) -> None:
    """List contacts."""

    contacts = _api_call("GET", f"/api/v1/contacts{_query_string(skip=skip, limit=limit, search=search)}")["data"]
    if format == "json":
        _echo_json(contacts)
        return
    if not contacts:
        typer.echo("No contacts found.")
        return
    _print_contact_table(contacts)


@app.command("get")
def contact_get(
    contact_id: Annotated[str, typer.Argument(help="Contact ID")],
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (text, json)")] = "text",
) -> None:
    """Get contact details."""

    contact = _api_call("GET", f"/api/v1/contacts/{contact_id}")["data"]
    if format == "json":
        _echo_json(contact)
        return
    typer.echo(f"ID: {contact['id']}")
    typer.echo(f"Name: {contact['name']}")
    typer.echo(f"Phone: {contact['phone_number']}")
    if contact.get("email"):
        typer.echo(f"Email: {contact['email']}")
    if contact.get("metadata"):
        typer.echo(f"Metadata: {json.dumps(contact['metadata'])}")


@app.command("update")
def contact_update(
    contact_id: Annotated[str, typer.Argument(help="Contact ID")],
    name: Annotated[Optional[str], typer.Option(help="Contact name")] = None,
    phone_number: Annotated[Optional[str], typer.Option("--phone-number", "--phone", "-p", help="Contact phone number")] = None,
    email: Annotated[Optional[str], typer.Option("--email", "-e", help="Contact email")] = None,
    metadata_json: Annotated[Optional[str], typer.Option("--metadata-json", help="Contact metadata as JSON object")] = None,
    channel: Annotated[Optional[str], typer.Option(help="Messaging channel for routing")] = None,
    chat_id: Annotated[Optional[str], typer.Option(help="Chat or room identifier for routing")] = None,
    session_key: Annotated[Optional[str], typer.Option(help="Session key for routing")] = None,
) -> None:
    """Update a contact."""

    payload = _build_contact_payload(
        name=name,
        phone_number=phone_number,
        email=email,
        metadata_json=metadata_json,
        channel=channel,
        chat_id=chat_id,
        session_key=session_key,
    )
    contact = _api_call("PUT", f"/api/v1/contacts/{contact_id}", payload)["data"]
    typer.echo(f"Updated contact: {contact['id']}")
    typer.echo(f"Name: {contact['name']}")
    typer.echo(f"Phone: {contact['phone_number']}")


@app.command("delete")
def contact_delete(contact_id: Annotated[str, typer.Argument(help="Contact ID")]) -> None:
    """Delete a contact."""

    _api_call("DELETE", f"/api/v1/contacts/{contact_id}")
    typer.echo(f"Contact deleted: {contact_id}")


@app.command("by-phone")
def contact_by_phone(
    phone_number: Annotated[str, typer.Argument(help="Contact phone number")],
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (text, json)")] = "text",
) -> None:
    """Find a contact by phone number."""

    contact = _api_call("GET", f"/api/v1/contacts/by-phone/{quote(phone_number, safe='')}")["data"]
    if format == "json":
        _echo_json(contact)
        return
    typer.echo(f"ID: {contact['id']}")
    typer.echo(f"Name: {contact['name']}")
    typer.echo(f"Phone: {contact['phone_number']}")


@app.command("by-email")
def contact_by_email(
    email: Annotated[str, typer.Argument(help="Contact email address")],
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (text, json)")] = "text",
) -> None:
    """Find a contact by email."""

    contact = _api_call("GET", f"/api/v1/contacts/by-email/{quote(email, safe='')}")["data"]
    if format == "json":
        _echo_json(contact)
        return
    typer.echo(f"ID: {contact['id']}")
    typer.echo(f"Name: {contact['name']}")
    typer.echo(f"Phone: {contact['phone_number']}")


@app.command("by-whatsapp-group")
def contact_by_whatsapp_group(
    group_id: Annotated[str, typer.Argument(help="WhatsApp group identifier")],
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (table, json)")] = "table",
) -> None:
    """List contacts in a WhatsApp group."""

    contacts = _api_call("GET", f"/api/v1/contacts/by-whatsapp-group/{quote(group_id, safe='')}")["data"]
    if format == "json":
        _echo_json(contacts)
        return
    if not contacts:
        typer.echo("No contacts found.")
        return
    _print_contact_table(contacts)


@app.command("set-default")
def contact_set_default(
    contact_id: Annotated[str, typer.Argument(help="Contact ID to set as default")],
) -> None:
    """Set a contact as the default for notifications."""

    contact = _api_call("PUT", f"/api/v1/contacts/{contact_id}/set-default", {})["data"]
    typer.echo(f"Default contact set: {contact['name']}")
    typer.echo(f"ID: {contact['id']}")
    typer.echo(f"Phone: {contact['phone_number']}")


@app.command("get-default")
def contact_get_default(
    format: Annotated[str, typer.Option("--format", "-f", help="Output format (text, json)")] = "text",
) -> None:
    """Get the current default contact for notifications."""

    try:
        contact = _api_call("GET", "/api/v1/contacts/default")["data"]
    except _HTTPError as e:
        if e.response.status_code == 404:
            typer.echo("No default contact configured.")
            return
        raise

    if format == "json":
        _echo_json(contact)
        return
    typer.echo(f"ID: {contact['id']}")
    typer.echo(f"Name: {contact['name']}")
    typer.echo(f"Phone: {contact['phone_number']}")


@app.command("clear-default")
def contact_clear_default() -> None:
    """Clear the default contact."""

    _api_call("DELETE", "/api/v1/contacts/default")
