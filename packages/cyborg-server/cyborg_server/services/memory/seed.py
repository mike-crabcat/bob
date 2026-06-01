"""Seed — regenerate bulletins from full session/DB history."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from cyborg_server.services.memory.channels import resolve_channel_id
from cyborg_server.services.memory.entity_resolver import canonical_contact_id
from cyborg_server.services.memory.models import parse_frontmatter
from cyborg_server.services.memory.bulletin_generator import build_generator_input, generate_bulletin, validate_draft_bulletin

logger = logging.getLogger(__name__)


async def seed_from_history(
    ctx: Any,
    workspace_dir: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Regenerate all memory from session history.

    1. Backs up old core/ directory
    2. Creates new v6 structure
    3. Reads all session messages from DB, grouped by session_key
    4. Feeds each session's transcript through the bulletin generator
    5. Writes validated bulletins to memory/bulletins/
    6. Runs dream pipeline on all generated bulletins
    """
    from cyborg_server.services.memory.service import MemoryService

    memory_dir = workspace_dir / "memory"

    # Step 1: Backup old core/ if it exists
    core_dir = memory_dir / "core"
    if core_dir.is_dir():
        backup_dir = memory_dir / "core.v1.bak"
        if not backup_dir.exists():
            logger.info("Backing up old core/ to core.v1.bak/")
            if not dry_run:
                shutil.move(str(core_dir), str(backup_dir))
        else:
            logger.info("core.v1.bak/ already exists, skipping backup")

    # Step 2: Ensure v6 structure
    if not dry_run:
        MemoryService.ensure_memory_structure(workspace_dir)

    # Step 3: Query all sessions from DB
    db = ctx.db

    # Get distinct session keys ordered by first message
    sessions = await db.fetch_all(
        "SELECT session_key, MIN(created_at) as first_message, MAX(created_at) as last_message, "
        "COUNT(*) as message_count "
        "FROM session_messages "
        "WHERE role IN ('user', 'assistant') "
        "GROUP BY session_key "
        "ORDER BY first_message ASC"
    )

    if not sessions:
        logger.info("No session messages found in DB")
        return {"status": "empty", "bulletins_generated": 0}

    logger.info("Found %d sessions to process", len(sessions))

    # Step 4: Load known contacts for entity resolution
    contacts = await db.fetch_all(
        "SELECT id, name FROM contacts WHERE name IS NOT NULL AND name != ''"
    )
    known_contacts = {
        canonical_contact_id(str(c["id"])): c["name"]
        for c in contacts
        if c["id"]
    }
    known_entities = {
        "contacts": [
            {"id": cid, "display_name": name}
            for cid, name in known_contacts.items()
        ]
    }

    # Step 5: Process each session
    svc = MemoryService(ctx)
    from cyborg_server.services.llm_dispatch import LLMDispatchService
    llm = LLMDispatchService(ctx)

    bulletins_generated = 0
    bulletins_skipped = 0
    errors = []

    for i, session in enumerate(sessions):
        session_key = session["session_key"]
        msg_count = session["message_count"]

        if msg_count < 3:
            # Skip very short sessions (likely noise)
            continue

        logger.info(
            "Processing session %d/%d: %s (%d messages)",
            i + 1, len(sessions), session_key, msg_count,
        )

        # Get messages for this session
        messages = await db.fetch_all(
            "SELECT role, content, sender_id, created_at "
            "FROM session_messages "
            "WHERE session_key = ? AND role IN ('user', 'assistant') "
            "ORDER BY created_at ASC",
            (session_key,),
        )

        if not messages:
            continue

        # Build transcript text
        transcript_parts = []
        participants_set: set[str] = set()

        for msg in messages:
            content = (msg["content"] or "").strip()
            if not content:
                continue

            # Add sender name for group chats
            sender = msg.get("sender_id", "")
            if sender:
                participants_set.add(sender)

            role = msg["role"]
            timestamp = msg.get("created_at", "")

            if sender and role == "user":
                # Try to resolve sender name
                sender_name = known_contacts.get(canonical_contact_id(sender), sender)
                transcript_parts.append(f"[{timestamp}] [{sender_name}]: {content}")
            else:
                transcript_parts.append(f"[{timestamp}] [assistant]: {content}")

        transcript_text = "\n".join(transcript_parts)

        # Skip if transcript is too short
        if len(transcript_text.strip()) < 50:
            continue

        # Split long transcripts into chunks instead of truncating
        max_chunk_len = 8000
        transcript_chunks: list[str] = []
        if len(transcript_text) <= max_chunk_len:
            transcript_chunks.append(transcript_text)
        else:
            lines = transcript_parts
            chunk = ""
            for line in lines:
                if chunk and len(chunk) + len(line) + 1 > max_chunk_len:
                    transcript_chunks.append(chunk)
                    chunk = line
                else:
                    chunk = (chunk + "\n" + line) if chunk else line
            if chunk:
                transcript_chunks.append(chunk)

        # Build generator input for each chunk
        first_time = session["first_message"]
        last_time = session["last_message"]
        contact_ids = list(participants_set)

        for chunk_idx, chunk_text in enumerate(transcript_chunks):
            chunk_label = f"chunk-{chunk_idx + 1}" if len(transcript_chunks) > 1 else ""

            if dry_run:
                logger.info(
                    "  Would generate bulletin for %s%s (%d chars transcript)",
                    session_key, f" [{chunk_label}]" if chunk_label else "",
                    len(chunk_text),
                )
                continue

            gen_input = build_generator_input(
                session_key=session_key,
                transcript_start=first_time,
                transcript_end=last_time,
                transcript_text=chunk_text,
                contact_ids=contact_ids,
                known_entities=known_entities,
            )

            # Generate bulletin
            try:
                response = await generate_bulletin(llm, gen_input)
                is_valid, data = validate_draft_bulletin(response)

                if not is_valid:
                    logger.warning(
                        "  Invalid bulletin response for %s%s: %s",
                        session_key, f" [{chunk_label}]" if chunk_label else "",
                        data.get("error", ""),
                    )
                    # Try to salvage: check if response contains useful content despite format issues
                    if response.strip().startswith("---"):
                        # Has some frontmatter, try relaxed parse
                        try:
                            from cyborg_server.services.memory.models import parse_frontmatter as _pf
                            fm, body = _pf(response.strip())
                            if fm.get("create_bulletin") is True or body.strip():
                                data = fm
                                data["create_bulletin"] = True
                                is_valid = True
                        except Exception:
                            pass
                    if not is_valid:
                        continue

                if data.get("create_bulletin") is False:
                    bulletins_skipped += 1
                    logger.debug(
                        "  No bulletin for %s%s: %s",
                        session_key, f" [{chunk_label}]" if chunk_label else "",
                        data.get("reason", ""),
                    )
                    continue

                # Extract content from the response
                _, body = parse_frontmatter(response)
                content = body.strip()
                if content.startswith("# Update"):
                    content = content[len("# Update"):].strip()

                # Write the bulletin
                channel_id = resolve_channel_id(session_key)
                path = svc.write_bulletin(
                    workspace_dir,
                    channel_id=channel_id,
                    source_type="seed",
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
                errors.append({"session_key": session_key, "error": str(exc)})

        if dry_run:
            continue

    # Step 6: Run dream pipeline on generated bulletins
    if not dry_run and bulletins_generated > 0:
        logger.info("Running dream pipeline on %d bulletins...", bulletins_generated)
        dream_result = await svc.run_dream(workspace_dir)
        logger.info("Dream complete: %s", dream_result)
    else:
        dream_result = {"status": "skipped"}

    return {
        "status": "completed",
        "sessions_processed": len(sessions),
        "bulletins_generated": bulletins_generated,
        "bulletins_skipped": bulletins_skipped,
        "errors": errors,
        "dream": dream_result,
    }
