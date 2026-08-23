"""Email tools for LLM function calling."""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
from pathlib import Path

from bob_server.context import AppContext
from bob_server.services.tools import Tool, tool

logger = logging.getLogger(__name__)


def _register_email_executors() -> None:
    """Bob3 Phase IV: email sends are effects. Executors call the delivery
    service, so test patches on EmailDeliveryService methods keep working."""
    from bob_server.services import effects as effects_svc

    async def _exec_reply(ctx, payload):
        from bob_server.services.email_delivery_service import EmailDeliveryService
        result = await EmailDeliveryService(ctx).send_reply(
            inbox_id=payload["inbox_id"], thread_id=payload["thread_id"],
            text=payload["text"], attachments=payload.get("attachments"))
        return (result or {}).get("message_id") or (result or {}).get("thread_id")

    async def _exec_send(ctx, payload):
        from bob_server.services.email_delivery_service import EmailDeliveryService
        result = await EmailDeliveryService(ctx).send_new_email(
            inbox_id=payload["inbox_id"], to=payload["to"],
            subject=payload["subject"], text=payload["text"],
            agenda=payload.get("agenda"),
            origin_session_key=payload.get("origin_session_key"),
            attachments=payload.get("attachments"))
        return (result or {}).get("thread_id")

    effects_svc.register_executor("email_reply", _exec_reply)
    effects_svc.register_executor("email_send", _exec_send)


_register_email_executors()

MAX_ATTACHMENT_SIZE = 25 * 1024 * 1024  # 25 MB

# Attachments we never persist to disk — executables and code files. Bob can
# bash to anything in his workspace, so downloaded code could be run; block at
# the source rather than rely on Bob's judgment.
_BLOCKED_ATTACHMENT_EXTENSIONS = {
    # Windows executables / installers / scripts
    ".exe", ".bat", ".cmd", ".com", ".scr", ".msi", ".ps1", ".vbs", ".wsf",
    ".cpl", ".hta",
    # Unix shells / scripts
    ".sh", ".bash", ".zsh", ".fish", ".ksh", ".csh", ".run", ".bin", ".command",
    ".tool", ".app",
    # Code files (Bob could execute or import these)
    ".py", ".pyw", ".pyc", ".pyo",
    ".js", ".mjs", ".cjs",
    ".ts", ".tsx", ".jsx",
    ".go", ".rs", ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hxx",
    ".java", ".class", ".jar",
    ".rb", ".php", ".pl", ".pm", ".lua",
    ".swift", ".kt", ".kts", ".scala", ".clj", ".cljs",
    ".r", ".jl",
    # Native libraries / bytecode / wasm
    ".dll", ".so", ".dylib", ".wasm",
    # Misc
    ".jar", ".war", ".ear",
}


def is_blocked_attachment(filename: str) -> bool:
    """True if the filename's extension is on the executable/code blocklist."""
    name = filename.lower().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    dot = name.rfind(".")
    if dot < 0:
        return False
    return name[dot:] in _BLOCKED_ATTACHMENT_EXTENSIONS


def _read_file_as_attachment(
    file_path: str,
    workspace_dir: Path,
) -> dict:
    """Read a file and return an attachment dict for the delivery service."""
    path = Path(file_path)
    if not path.is_absolute():
        path = workspace_dir / path
    path = path.resolve()

    if not path.is_file():
        raise FileNotFoundError(f"File not found: {file_path}")
    if not str(path).startswith(str(workspace_dir.resolve())):
        raise ValueError(f"File must be within the workspace: {file_path}")

    size = path.stat().st_size
    if size > MAX_ATTACHMENT_SIZE:
        raise ValueError(f"File too large ({size} bytes, max {MAX_ATTACHMENT_SIZE}): {file_path}")

    content = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "content": content,
        "filename": path.name,
        "content_type": mimetypes.guess_type(str(path))[0] or "application/octet-stream",
    }


def make_email_tools(
    ctx: AppContext,
    thread_id: str,
    inbox_id: str,
    *,
    reply_tracker: list | None = None,
    reply_body_tracker: list | None = None,
    inbox_agentmail_id: str = "",
):
    """Create email reply/skip tools bound to the given thread.

    If reply_tracker is provided, email_reply sets tracker[0] = True
    so callers can detect whether a reply was sent.
    If reply_body_tracker is provided, email_reply appends the body text.
    """

    @tool
    async def email_reply(body: str, attachments: list[str] | None = None) -> str:
        """Send a reply to the current email thread. Always use this tool to respond — do not just generate text output. Optionally attach files by providing their paths as a list (workspace-relative or absolute)."""
        from bob_server.services.email_delivery_service import EmailDeliveryService

        if isinstance(attachments, str):
            s = attachments.strip()
            attachments = None if not s or s == "[]" else [s]

        settings = ctx.settings
        workspace_dir = settings.harness.workspace_dir.expanduser().resolve()

        attachment_dicts = None
        if attachments:
            attachments = [fp for fp in attachments if fp and fp.strip()]
        if attachments:
            attachment_dicts = []
            errors = []
            for fp in attachments:
                try:
                    attachment_dicts.append(_read_file_as_attachment(fp, workspace_dir))
                except (FileNotFoundError, ValueError) as e:
                    errors.append(str(e))
            if errors:
                return f"Error with attachments: {'; '.join(errors)}"

        from uuid import uuid4 as _uuid4

        from bob_server.services.effects import emit_and_deliver

        outcome = await emit_and_deliver(
            ctx, kind="email_reply",
            idempotency_key=f"email_reply:{thread_id}:{_uuid4().hex}",
            payload={"inbox_id": inbox_id, "thread_id": thread_id,
                     "text": body, "attachments": attachment_dicts})
        if not outcome.get("ok"):
            logger.warning("email_reply failed: %s", outcome.get("error"))
            return f"Error sending reply: {outcome.get('error', 'delivery failed')}"
        if reply_tracker is not None:
            reply_tracker[0] = True
        if reply_body_tracker is not None:
            reply_body_tracker.append(body)
        result = {"ok": True, "thread_id": thread_id}
        if attachment_dicts:
            result["attachments_sent"] = [a["filename"] for a in attachment_dicts]
        return json.dumps(result)

    @tool
    async def email_skip() -> str:
        """Skip replying to this email — no response is needed."""
        return json.dumps({"ok": True, "skipped": True})

    @tool
    async def list_attachments() -> str:
        """List all attachments across all messages in this email thread.
        Shows filename, content type, size, download status, and attachment_id.
        Use download_attachment with the attachment_id to save a file to the workspace."""
        from bob_server.services.email_store import EmailStore
        messages = await EmailStore(ctx.db).attachment_messages(thread_id)

        if not messages:
            return json.dumps({"attachments": [], "message": "No attachments found in this thread"})

        all_attachments = []
        for msg in messages:
            att_json = msg.get("attachments_json")
            if not att_json:
                continue
            try:
                attachments = json.loads(att_json)
            except (ValueError, TypeError):
                continue

            for att in attachments:
                fname = att.get("filename", "")
                all_attachments.append({
                    "attachment_id": att.get("attachment_id", ""),
                    "filename": fname,
                    "content_type": att.get("content_type", ""),
                    "size": att.get("size"),
                    "downloaded": att.get("downloaded", False),
                    "path": att.get("path"),
                    "blocked": is_blocked_attachment(fname),
                    "from_message": {
                        "sender": msg.get("sender_name") or msg.get("sender_email"),
                        "subject": msg.get("subject"),
                        "timestamp": msg.get("message_timestamp"),
                    },
                })

        return json.dumps({
            "thread_id": thread_id,
            "total_attachments": len(all_attachments),
            "attachments": all_attachments,
        })

    @tool
    async def download_attachment(attachment_id: str) -> str:
        """Download an email attachment to the workspace directory.
        Use list_attachments first to find the attachment_id. Blocked for
        executables and code files (they are filtered at the source); all other
        types are available to any sender — Bob decides what to open."""
        if not attachment_id:
            return "Error: attachment_id is required."

        # Find the message that owns this attachment_id
        from bob_server.services.email_store import EmailStore
        _store = EmailStore(ctx.db)
        messages = await _store.attachment_messages(thread_id)

        target_msg_id = None
        target_att = None
        for msg in messages:
            att_json = msg.get("attachments_json")
            if not att_json:
                continue
            try:
                attachments = json.loads(att_json)
            except (ValueError, TypeError):
                continue
            for att in attachments:
                if att.get("attachment_id") == attachment_id:
                    target_msg_id = msg["agentmail_message_id"]
                    target_att = att
                    break
            if target_msg_id:
                break

        if not target_msg_id or not target_att:
            return f"Error: Attachment {attachment_id} not found in this thread."

        filename = target_att.get("filename", attachment_id)
        if is_blocked_attachment(filename):
            return f"Error: '{filename}' is an executable or code file and is blocked. Bob decides what to open, but code/executable payloads are never persisted to disk."

        # Already downloaded?
        if target_att.get("downloaded") and target_att.get("path"):
            existing_path = Path(target_att["path"])
            if existing_path.exists():
                return json.dumps({
                    "ok": True,
                    "path": target_att["path"],
                    "filename": filename,
                    "message": "Already downloaded",
                })

        # Download via AgentMail
        from bob_server.services.agentmail_client import AgentMailClient

        settings = ctx.settings
        client = AgentMailClient(
            base_url=settings.agentmail.base_url,
            api_key=settings.agentmail.api_key,
        )

        try:
            content = await client.get_attachment(
                inbox_agentmail_id,
                target_msg_id,
                attachment_id,
            )
        except Exception as e:
            logger.warning("Failed to download attachment %s: %s", attachment_id, e)
            return f"Error downloading attachment: {e}"
        finally:
            await client.close()

        # Save to workspace
        workspace_dir = settings.harness.workspace_dir.expanduser().resolve()
        dest_dir = workspace_dir / "email-attachments" / thread_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename

        # Avoid overwriting by appending suffix if needed
        counter = 1
        base_dest = dest
        while dest.exists():
            stem = base_dest.stem
            suffix = base_dest.suffix
            dest = dest_dir / f"{stem}_{counter}{suffix}"
            counter += 1

        dest.write_bytes(content)

        # Update attachments_json to mark as downloaded
        existing_json = await _store.attachments_json_of(target_msg_id)
        if existing_json:
            try:
                atts = json.loads(existing_json)
                for att in atts:
                    if att.get("attachment_id") == attachment_id:
                        att["downloaded"] = True
                        att["path"] = str(dest)
                        att["size"] = len(content)
                await _store.set_attachments_json_by_agentmail(
                    target_msg_id, json.dumps(atts))
            except (ValueError, TypeError):
                pass

        return json.dumps({
            "ok": True,
            "filename": filename,
            "size": len(content),
            "path": str(dest),
        })

    return [email_reply, email_skip, list_attachments, download_attachment]


def make_email_send_tools(ctx: AppContext, *, session_key: str | None = None) -> list[Tool]:
    """Create email_send tool for initiating new email threads. Not bound to a specific thread."""

    @tool
    async def email_send(
        to: str,
        subject: str,
        body: str,
        agenda: str,
        attachments: list[str] | None = None,
    ) -> str:
        """Send a new email to start a conversation with someone. Use this to proactively reach out to a contact by email (follow up, schedule, begin a discussion). The agenda describes the purpose and guides all future responses in this thread. The recipient email address must be known. Optionally attach files by providing their paths as a list (workspace-relative or absolute)."""
        from bob_server.services.email_delivery_service import EmailDeliveryService

        if isinstance(attachments, str):
            s = attachments.strip()
            attachments = None if not s or s == "[]" else [s]

        settings = ctx.settings
        workspace_dir = settings.harness.workspace_dir.expanduser().resolve()

        attachment_dicts = None
        if attachments:
            attachments = [fp for fp in attachments if fp and fp.strip()]
        if attachments:
            attachment_dicts = []
            errors = []
            for fp in attachments:
                try:
                    attachment_dicts.append(_read_file_as_attachment(fp, workspace_dir))
                except (FileNotFoundError, ValueError) as e:
                    errors.append(str(e))
            if errors:
                return f"Error with attachments: {'; '.join(errors)}"

        # Resolve default inbox
        from bob_server.services.email_store import EmailStore
        inbox = await EmailStore(ctx.db).first_active_inbox()
        if inbox is None:
            return "Error: no active email inbox configured"

        from uuid import uuid4 as _uuid4

        from bob_server.services.effects import emit_and_deliver

        outcome = await emit_and_deliver(
            ctx, kind="email_send",
            idempotency_key=f"email_send:{_uuid4().hex}",
            payload={"inbox_id": inbox["id"], "to": to, "subject": subject,
                     "text": body, "agenda": agenda,
                     "origin_session_key": session_key,
                     "attachments": attachment_dicts})
        if not outcome.get("ok"):
            logger.warning("email_send failed: %s", outcome.get("error"))
            return f"Error sending email: {outcome.get('error', 'delivery failed')}"
        response = {"ok": True, "thread_id": outcome.get("external_result_id", "")}
        if attachment_dicts:
            response["attachments_sent"] = [a["filename"] for a in attachment_dicts]
        return json.dumps(response)

    return [email_send]


def make_email_thread_tools(
    ctx: AppContext, *, contact_id: str | None = None, is_trusted: bool = False,
) -> list[Tool]:
    """Create email thread tools (read + search).

    Args:
        contact_id: The current session's contact. Used to scope search results
            for untrusted contacts to only their own threads.
        is_trusted: If True, search returns all threads. If False, only threads
            belonging to contact_id.
    """

    @tool
    async def email_thread_read(thread_id: str) -> str:
        """Read the full transcript of an email thread by its agentmail thread ID.
        Returns subject, participants, and all messages in chronological order.
        Use this to look up the original context behind an email-sourced memory bulletin."""
        from bob_server.services.email_store import EmailStore
        store = EmailStore(ctx.db)
        thread = await store.thread_by_agentmail_any(thread_id)
        if thread is None:
            return json.dumps({"error": f"Thread not found: {thread_id}"})

        messages = await store.thread_messages(thread_id)

        # Resolve inbox address to detect outbound messages
        inbox = await store.get_inbox(thread["inbox_id"], include_deleted=True)
        inbox_email = (inbox["email_address"] or "").lower() if inbox else ""

        lines = []
        for msg in messages:
            text = (msg.get("text_body") or "").strip()
            if not text:
                continue
            sender_email = (msg.get("sender_email") or "").lower()
            sender_name = msg.get("sender_name") or msg.get("sender_email") or "Unknown"
            subject = msg.get("subject", "(no subject)")
            ts = msg.get("message_timestamp", "")
            role = "assistant" if sender_email == inbox_email else sender_name
            lines.append(f"[{ts}] [{role}] [Subject: {subject}]\n{text}")

        return json.dumps({
            "thread_id": thread["agentmail_thread_id"],
            "subject": thread.get("subject", ""),
            "contact_id": thread.get("contact_id"),
            "message_count": len(messages),
            "transcript": "\n\n".join(lines),
        })

    @tool
    async def email_thread_search(query: str) -> str:
        """Search email threads by keyword. Returns a ranked list of matching threads
        with thread_id, subject, contact name, message count, and last message date.
        Use email_thread_read with the thread_id to get the full transcript."""
        import re

        terms = [t for t in re.split(r"\s+", query.strip()) if t]
        if not terms:
            return json.dumps({"error": "Empty query", "results": []})

        from bob_server.services.email_store import EmailStore
        # Scope: untrusted contacts only see their own threads
        scope_contact = contact_id if (not is_trusted and contact_id) else None
        rows = await EmailStore(ctx.db).search_threads(terms, contact_id=scope_contact)

        results = []
        for row in rows:
            results.append({
                "thread_id": row["agentmail_thread_id"],
                "subject": row.get("subject", ""),
                "contact_name": row.get("contact_name"),
                "message_count": row.get("message_count", 0),
                "matching_messages": row.get("matching_messages", 0),
                "last_message_at": row.get("last_message_at"),
            })

        return json.dumps({"query": query, "result_count": len(results), "results": results})

    return [email_thread_read, email_thread_search]


def make_email_thread_result_tools(
    ctx: AppContext,
    *,
    thread_id: str,
    origin_session_key: str,
    agenda: str,
    wa_service: object | None = None,
) -> list:
    """finish_email_thread for a thread with an origin session: completing
    relays the result by waking the origin conversation (Bob3 Phase V wake
    path — channel-agnostic). ``wa_service`` is accepted for backward
    compatibility and unused."""

    @tool
    async def finish_email_thread(result: str) -> str:
        """Complete the email thread task and relay the result back to the requesting session.
        Call when you have achieved the objective from the email conversation.
        The result will be dispatched to the originating session, which will decide
        how to relay it."""
        from bob_server.services.wake_service import wake_conversation

        db = ctx.db
        from bob_server.services.email_store import EmailStore
        store = EmailStore(db)
        thread_row = await store.thread_by_id_or_agentmail(thread_id)
        subject = thread_row["subject"] if thread_row else "unknown"
        contact_name = "unknown"
        if thread_row and thread_row.get("contact_id"):
            from bob_server.repositories.contacts import ContactRepository
            contact = await ContactRepository(db).get(thread_row["contact_id"])
            if contact:
                contact_name = contact["name"]

        await store.clear_thread_origin(thread_id)

        result_content = (
            f"## Email Thread Result\n"
            f"Subject: {subject}\n"
            f"Contact: {contact_name}\n"
            f"Agenda: {agenda}\n\n"
            f"{result}"
        )
        await wake_conversation(
            ctx, origin_session_key, result_content,
            call_category="email_thread_result",
        )
        logger.info(
            "Email thread result dispatched from thread %s to origin session %s",
            thread_id, origin_session_key,
        )
        return json.dumps({"ok": True, "dispatched_to": origin_session_key})

    return [finish_email_thread]
