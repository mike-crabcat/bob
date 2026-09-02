"""Bob CLI email subapp."""

from __future__ import annotations

from bob_server.cli._helpers import *  # noqa: F403,F405

# ---------------------------------------------------------------------------
# Email relay
# ---------------------------------------------------------------------------

app = typer.Typer(help="Email relay operations")
inbox_app = typer.Typer(help="Email inbox management")
app.add_typer(inbox_app, name="inbox")



def _read_file_as_attachment(file_path: str, *, inline: bool = False) -> dict[str, Any]:
    path = Path(file_path)
    if not path.is_file():
        raise typer.BadParameter(f"File not found: {file_path}")
    content = base64.b64encode(path.read_bytes()).decode("ascii")
    result: dict[str, Any] = {
        "content": content,
        "filename": path.name,
        "content_type": mimetypes.guess_type(str(path))[0] or "application/octet-stream",
        "content_disposition": "inline" if inline else "attachment",
    }
    if inline:
        result["content_id"] = path.name
    return result


@inbox_app.command("register")
def email_inbox_register(
    agentmail_inbox_id: Annotated[str, typer.Option("--agentmail-inbox-id", help="AgentMail inbox ID")],
    display_name: Annotated[str, typer.Option("--display-name", help="Display name for this inbox")],
    email_address: Annotated[str, typer.Option("--email-address", help="Email address for this inbox")],
    metadata_json: Annotated[Optional[str], typer.Option("--metadata-json", help="Optional metadata JSON")] = None,
) -> None:
    """Register an AgentMail inbox for email relay."""
    payload: dict[str, Any] = {
        "agentmail_inbox_id": agentmail_inbox_id,
        "display_name": display_name,
        "email_address": email_address,
    }
    if metadata_json:
        payload["metadata"] = _parse_json_option(metadata_json, "metadata-json", dict)
    result = _api_call("POST", "/api/v1/email/inboxes", payload)
    _echo_json(result["data"])


@inbox_app.command("list")
def email_inbox_list() -> None:
    """List registered email inboxes."""
    result = _api_call("GET", "/api/v1/email/inboxes")
    _echo_json(result["data"])


@inbox_app.command("get")
def email_inbox_get(
    id: Annotated[str, typer.Argument(help="Inbox ID")],
) -> None:
    """Get a registered email inbox."""
    result = _api_call("GET", f"/api/v1/email/inboxes/{id}")
    _echo_json(result["data"])


@inbox_app.command("remove")
def email_inbox_remove(
    id: Annotated[str, typer.Argument(help="Inbox ID")],
) -> None:
    """Remove a registered email inbox."""
    _api_call("DELETE", f"/api/v1/email/inboxes/{id}")
    typer.echo("Inbox removed.")


@app.command("send")
def email_send(
    to: Annotated[str, typer.Option("--to", help="Recipient email address")],
    subject: Annotated[str, typer.Option("--subject", help="Email subject")],
    text: Annotated[str, typer.Option("--text", help="Email body text")],
    agenda: Annotated[str, typer.Option("--agenda", help="Purpose and handling instructions for this email thread (required)")],
    cc: Annotated[Optional[list[str]], typer.Option("--cc", help="CC recipients")] = None,
    html: Annotated[Optional[str], typer.Option("--html", help="HTML body (use cid: references for inline images)")] = None,
    attach: Annotated[Optional[list[str]], typer.Option("--attach", help="File path to attach (repeatable)")] = None,
    inline_image: Annotated[Optional[list[str]], typer.Option("--inline-image", help="Inline image file path (repeatable)")] = None,
    inbox_id: Annotated[Optional[str], typer.Option("--inbox", help="Inbox ID (default: auto-resolve)")] = None,
) -> None:
    """Send a new email from a registered inbox."""
    resolved_inbox = _resolve_inbox_id(inbox_id)
    payload: dict[str, Any] = {"to": to, "subject": subject, "text": text, "agenda": agenda}
    if cc:
        payload["cc"] = cc
    if html:
        payload["html"] = html
    attachments: list[dict[str, Any]] = []
    if attach:
        for fp in attach:
            attachments.append(_read_file_as_attachment(fp))
    if inline_image:
        for fp in inline_image:
            attachments.append(_read_file_as_attachment(fp, inline=True))
    if attachments:
        payload["attachments"] = attachments
    result = _api_call("POST", f"/api/v1/email/inboxes/{resolved_inbox}/send", payload)
    _echo_json(result.get("data", result))


@app.command("reply")
def email_reply(
    message_id: Annotated[str, typer.Option("--message-id", help="Message ID to reply to")],
    text: Annotated[str, typer.Option("--text", help="Reply body text")],
    reply_all: Annotated[bool, typer.Option("--reply-all", help="Reply to all recipients")] = False,
    html: Annotated[Optional[str], typer.Option("--html", help="HTML body (use cid: references for inline images)")] = None,
    attach: Annotated[Optional[list[str]], typer.Option("--attach", help="File path to attach (repeatable)")] = None,
    inline_image: Annotated[Optional[list[str]], typer.Option("--inline-image", help="Inline image file path (repeatable)")] = None,
    inbox_id: Annotated[Optional[str], typer.Option("--inbox", help="Inbox ID (default: auto-resolve)")] = None,
) -> None:
    """Reply to an email message."""
    resolved_inbox = _resolve_inbox_id(inbox_id)
    payload: dict[str, Any] = {"message_id": message_id, "text": text, "reply_all": reply_all}
    if html:
        payload["html"] = html
    attachments: list[dict[str, Any]] = []
    if attach:
        for fp in attach:
            attachments.append(_read_file_as_attachment(fp))
    if inline_image:
        for fp in inline_image:
            attachments.append(_read_file_as_attachment(fp, inline=True))
    if attachments:
        payload["attachments"] = attachments
    result = _api_call("POST", f"/api/v1/email/inboxes/{resolved_inbox}/reply", payload)
    _echo_json(result.get("data", result))


@app.command("messages")
def email_messages(
    limit: Annotated[int, typer.Option("--limit", help="Max messages")] = 25,
    inbox_id: Annotated[Optional[str], typer.Option("--inbox", help="Inbox ID (default: auto-resolve)")] = None,
) -> None:
    """List messages in an inbox."""
    resolved_inbox = _resolve_inbox_id(inbox_id)
    result = _api_call("GET", f"/api/v1/email/inboxes/{resolved_inbox}/messages?limit={limit}")
    _echo_json(result.get("data", result))


@app.command("download-attachment")
def email_download_attachment(
    message_id: Annotated[str, typer.Option("--message-id", help="AgentMail message ID")],
    attachment_id: Annotated[str, typer.Option("--attachment-id", help="Attachment ID")],
    output: Annotated[str, typer.Option("--output", "-o", help="Output file path")] = "",
    inbox_id: Annotated[Optional[str], typer.Option("--inbox", help="Inbox ID (default: auto-resolve)")] = None,
) -> None:
    """Download an email attachment to disk."""
    resolved_inbox = _resolve_inbox_id(inbox_id)
    settings = Settings.from_env()
    encoded_msg = quote(message_id, safe="")
    url = f"http://{settings.host}:{settings.port}/api/v1/email/inboxes/{resolved_inbox}/messages/{encoded_msg}/attachments/{attachment_id}"
    req = Request(url, method="GET")

    try:
        with urlopen(req, timeout=60) as response:
            content = response.read()
    except HTTPError as exc:
        _handle_http_error(exc)
    except URLError as exc:
        _handle_connection_error(exc)

    output_path = Path(output) if output else Path(attachment_id)
    if output and output_path.is_dir():
        output_path = output_path / attachment_id
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(content)
    _echo_json({"path": str(output_path.resolve()), "size": len(content)})


@app.command("threads")
def email_threads(
    inbox_id: Annotated[Optional[str], typer.Option("--inbox", help="Filter by inbox ID")] = None,
) -> None:
    """List tracked email threads."""
    qs = _query_string(inbox_id=inbox_id)
    result = _api_call("GET", f"/api/v1/email/threads{qs}")
    _echo_json(result["data"])


@app.command("thread")
def email_thread_get(
    thread_id: Annotated[str, typer.Argument(help="Thread ID")],
) -> None:
    """Get a tracked email thread."""
    result = _api_call("GET", f"/api/v1/email/threads/{thread_id}")
    _echo_json(result["data"])


@app.command("update-agenda")
def email_thread_update_agenda(
    thread_id: Annotated[str, typer.Argument(help="Thread ID")],
    agenda: Annotated[str, typer.Option("--agenda", help="New agenda text for the thread")],
) -> None:
    """Update the agenda for an email thread."""
    result = _api_call("PATCH", f"/api/v1/email/threads/{thread_id}/agenda", {"agenda": agenda})
    _echo_json(result.get("data", result))


@app.command("sync")
def email_sync() -> None:
    """Sync all inboxes — fetch missing messages from AgentMail and persist locally."""
    result = _api_call("POST", "/api/v1/email/sync")
    data = result.get("data", result)
    count = data.get("synced", 0)
    typer.echo(f"Synced {count} message(s) from AgentMail")

