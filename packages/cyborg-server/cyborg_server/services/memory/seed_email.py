"""Seed-email — regenerate bulletins from email thread history."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from cyborg_server.services.memory.channels import resolve_channel_id
from cyborg_server.services.memory.entity_resolver import canonical_contact_id
from cyborg_server.services.memory.models import parse_frontmatter
from cyborg_server.services.memory.bulletin_generator import build_generator_input, generate_bulletin, validate_draft_bulletin

logger = logging.getLogger(__name__)


def _build_session_key(thread_id: str) -> str:
    return f"agent:main:email:thread:{thread_id}"


async def seed_from_email_history(
    ctx: Any,
    workspace_dir: Path,
    *,
    dry_run: bool = False,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """Regenerate memory from email thread history.

    1. Queries tracked email threads from the database
    2. For each thread, fetches all email_messages
    3. Builds a transcript with email headers
    4. Feeds each thread's transcript through the bulletin generator
    5. Writes validated bulletins
    6. Runs dream pipeline
    """
    from cyborg_server.services.memory.service import MemoryService
    from cyborg_server.services.llm_dispatch import LLMDispatchService

    if not dry_run:
        MemoryService.ensure_memory_structure(workspace_dir)

    db = ctx.db

    if thread_id:
        threads = await db.fetch_all(
            """SELECT et.*, ei.email_address as inbox_email
               FROM email_threads et
               JOIN email_inboxes ei ON ei.id = et.inbox_id
               WHERE et.agentmail_thread_id = ? AND et.deleted_at IS NULL""",
            (thread_id,),
        )
    else:
        threads = await db.fetch_all(
            """SELECT et.*, ei.email_address as inbox_email
               FROM email_threads et
               JOIN email_inboxes ei ON ei.id = et.inbox_id
               WHERE et.deleted_at IS NULL AND et.is_active = 1
               ORDER BY et.last_message_at ASC""",
        )

    if not threads:
        logger.info("No email threads found")
        return {"status": "empty", "bulletins_generated": 0}

    logger.info("Found %d email threads to process", len(threads))

    contacts = await db.fetch_all(
        "SELECT id, name, email FROM contacts WHERE name IS NOT NULL AND name != ''"
    )
    known_contacts: dict[str, str] = {}
    email_to_contact: dict[str, str] = {}
    for c in contacts:
        cid = canonical_contact_id(str(c["id"]))
        known_contacts[cid] = c["name"]
        if c.get("email"):
            email_to_contact[c["email"].lower()] = cid

    known_entities = {
        "contacts": [
            {"id": cid, "display_name": name}
            for cid, name in known_contacts.items()
        ]
    }

    svc = MemoryService(ctx)
    llm = LLMDispatchService(ctx)

    bulletins_generated = 0
    bulletins_skipped = 0
    errors: list[dict[str, str]] = []

    for i, thread in enumerate(threads):
        agentmail_thread_id = thread["agentmail_thread_id"]
        session_key = _build_session_key(agentmail_thread_id)

        messages = await db.fetch_all(
            """SELECT sender_email, sender_name, text_body, subject,
                      message_timestamp
               FROM email_messages
               WHERE thread_id = ?
               ORDER BY message_timestamp ASC""",
            (agentmail_thread_id,),
        )

        if not messages or len(messages) < 2:
            continue

        logger.info(
            "Processing thread %d/%d: %s (%d messages)",
            i + 1, len(threads), agentmail_thread_id, len(messages),
        )

        inbox_email = (thread.get("inbox_email") or "").lower()
        transcript_parts: list[str] = []
        contact_ids_set: set[str] = set()
        first_timestamp: str | None = None
        last_timestamp: str | None = None

        for msg in messages:
            text = (msg.get("text_body") or "").strip()
            if not text:
                continue

            sender_email = msg.get("sender_email", "unknown")
            sender_name = msg.get("sender_name") or sender_email
            subject = msg.get("subject", "(no subject)")
            timestamp = msg.get("message_timestamp", "")

            if first_timestamp is None:
                first_timestamp = timestamp
            last_timestamp = timestamp

            contact_id = email_to_contact.get(sender_email.lower())
            if contact_id:
                contact_ids_set.add(contact_id)
                sender_label = known_contacts.get(contact_id, sender_name)
            else:
                sender_label = sender_name

            is_outbound = sender_email.lower() == inbox_email
            role_label = "assistant" if is_outbound else sender_label

            transcript_parts.append(
                f"[{timestamp}] [{role_label}] [Subject: {subject}]\n{text[:2000]}"
            )

        if not transcript_parts:
            continue

        transcript_text = "\n\n".join(transcript_parts)

        if len(transcript_text.strip()) < 50:
            continue

        max_chunk_len = 8000
        transcript_chunks: list[str] = []
        if len(transcript_text) <= max_chunk_len:
            transcript_chunks.append(transcript_text)
        else:
            chunk = ""
            for part in transcript_parts:
                if chunk and len(chunk) + len(part) + 2 > max_chunk_len:
                    transcript_chunks.append(chunk)
                    chunk = part
                else:
                    chunk = (chunk + "\n\n" + part) if chunk else part
            if chunk:
                transcript_chunks.append(chunk)

        contact_ids = list(contact_ids_set)

        for chunk_idx, chunk_text in enumerate(transcript_chunks):
            chunk_label = f"chunk-{chunk_idx + 1}" if len(transcript_chunks) > 1 else ""

            if dry_run:
                logger.info(
                    "  Would generate bulletin for %s%s (%d chars transcript)",
                    session_key,
                    f" [{chunk_label}]" if chunk_label else "",
                    len(chunk_text),
                )
                continue

            gen_input = build_generator_input(
                session_key=session_key,
                transcript_start=first_timestamp or "",
                transcript_end=last_timestamp or "",
                transcript_text=chunk_text,
                contact_ids=contact_ids,
                known_entities=known_entities,
            )

            try:
                response = await generate_bulletin(llm, gen_input)
                is_valid, data = validate_draft_bulletin(response)

                if not is_valid:
                    if response.strip().startswith("---"):
                        try:
                            fm, body = parse_frontmatter(response.strip())
                            if fm.get("create_bulletin") is True or body.strip():
                                data = fm
                                data["create_bulletin"] = True
                                is_valid = True
                        except Exception:
                            pass
                    if not is_valid:
                        logger.warning(
                            "  Invalid bulletin for %s%s",
                            session_key,
                            f" [{chunk_label}]" if chunk_label else "",
                        )
                        continue

                if data.get("create_bulletin") is False:
                    bulletins_skipped += 1
                    continue

                _, body = parse_frontmatter(response.strip())
                content = body.strip()
                if content.startswith("# Update"):
                    content = content[len("# Update"):].strip()

                channel_id = resolve_channel_id(session_key)
                path = svc.write_bulletin(
                    workspace_dir,
                    channel_id=channel_id,
                    source_type="email_seed",
                    source_id=session_key,
                    content=content,
                    visibility=data.get("visibility", "private"),
                    scope=data.get("scope", []),
                    entities=data.get("entities", {}),
                )
                bulletins_generated += 1
                logger.info("  Bulletin written: %s", path)

            except Exception as exc:
                logger.exception("Error generating bulletin for %s", session_key)
                errors.append({"thread_id": agentmail_thread_id, "error": str(exc)})

    if not dry_run and bulletins_generated > 0:
        logger.info("Running dream pipeline on %d bulletins...", bulletins_generated)
        dream_result = await svc.run_dream(workspace_dir)
    else:
        dream_result = {"status": "skipped"}

    return {
        "status": "completed",
        "threads_processed": len(threads),
        "bulletins_generated": bulletins_generated,
        "bulletins_skipped": bulletins_skipped,
        "errors": errors,
        "dream": dream_result,
    }
