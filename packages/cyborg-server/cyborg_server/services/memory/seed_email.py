"""Seed-email — regenerate bulletins from email history."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from cyborg_server.services.memory.channels import resolve_channel_id, derive_visibility
from cyborg_server.services.memory.entity_resolver import canonical_contact_id
from cyborg_server.services.memory.bulletin_generator import build_generator_input, generate_bulletins

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
    """Regenerate memory from email history."""
    from cyborg_server.services.memory.service import MemoryService
    from cyborg_server.services.llm_dispatch import LLMDispatchService

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

    svc = MemoryService(ctx)
    llm = LLMDispatchService(ctx)

    bulletins_generated = 0
    errors: list[dict[str, str]] = []

    for i, thread in enumerate(threads):
        agentmail_thread_id = thread["agentmail_thread_id"]
        session_key = _build_session_key(agentmail_thread_id)
        inbox_email = (thread.get("inbox_email") or "").lower()

        messages = await db.fetch_all(
            """SELECT sender_email, sender_name, text_body, subject,
                      message_timestamp
               FROM email_messages
               WHERE thread_id = ?
               ORDER BY message_timestamp ASC""",
            (agentmail_thread_id,),
        )

        if not messages:
            continue

        logger.info(
            "Processing thread %d/%d: %s (%d messages)",
            i + 1, len(threads), agentmail_thread_id, len(messages),
        )

        participants: list[dict[str, str]] = []
        seen_ids: set[str] = set()

        gen_messages = []
        for msg in messages:
            text = (msg.get("text_body") or "").strip()
            if not text:
                continue

            sender_email = msg.get("sender_email", "unknown")
            sender_name = msg.get("sender_name") or sender_email
            timestamp = msg.get("message_timestamp", "")

            contact_id = email_to_contact.get(sender_email.lower())
            is_outbound = sender_email.lower() == inbox_email

            if contact_id:
                sender_label = contact_id
                if contact_id not in seen_ids:
                    participants.append({"id": contact_id, "name": known_contacts.get(contact_id, sender_name)})
                    seen_ids.add(contact_id)
            else:
                sender_label = "assistant" if is_outbound else sender_email

            gen_messages.append({
                "sender_contact_id": sender_label,
                "timestamp": timestamp,
                "content": text[:500],
            })

        if not gen_messages:
            continue

        if dry_run:
            logger.info("  Would generate bulletins for %s", session_key)
            continue

        channel_id = resolve_channel_id(session_key)
        visibility = derive_visibility(session_key)

        gen_input = build_generator_input(
            session_key=session_key,
            messages=gen_messages,
            participants=participants,
        )

        try:
            bulletin_texts = await generate_bulletins(llm, gen_input)
            last_msg_ts = gen_messages[-1].get("timestamp", "") if gen_messages else None
            for text in bulletin_texts:
                await svc.write_bulletin(
                    workspace_dir,
                    channel_id=channel_id,
                    source_type="email_seed",
                    source_id=session_key,
                    content=text,
                    visibility=visibility,
                    occurred_at=last_msg_ts,
                )
                bulletins_generated += 1

            if bulletin_texts:
                logger.info("  Generated %d bulletin(s)", len(bulletin_texts))

        except Exception as exc:
            logger.exception("Error generating bulletins for %s", session_key)
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
        "errors": errors,
        "dream": dream_result,
    }
