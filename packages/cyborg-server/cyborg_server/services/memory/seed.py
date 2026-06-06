"""Seed — regenerate bulletins from full session/DB history."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from cyborg_server.services.memory.channels import resolve_channel_id, derive_visibility
from cyborg_server.services.memory.entity_resolver import canonical_contact_id

logger = logging.getLogger(__name__)


async def seed_from_history(
    ctx: Any,
    workspace_dir: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Regenerate all memory from session history.

    1. Clears existing memory tables
    2. Reads all session messages from DB, grouped by session_key
    3. Feeds each session's transcript through the bulletin generator
    4. Writes plain-text bulletins
    5. Runs dream pipeline on all generated bulletins
    """
    from cyborg_server.services.memory.service import MemoryService
    from cyborg_server.services.memory.bulletin_generator import (
        build_generator_input,
        generate_bulletins,
    )
    from cyborg_server.services.llm_dispatch import LLMDispatchService

    db = ctx.db

    # Step 1: Clear existing memory
    if not dry_run:
        for table in ("memory_claims", "memory_claim_bulletins", "memory_entity_relations",
                       "memory_entity_bulletins", "memory_aliases", "memory_entities"):
            await db.execute(f"DELETE FROM {table}")
        await db.execute("DELETE FROM memory_bulletins")
        logger.info("Cleared existing memory tables")

    # Step 2: Query all sessions from DB
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

    # Step 3: Load known contacts for participant resolution
    contacts = await db.fetch_all(
        "SELECT id, name FROM contacts WHERE name IS NOT NULL AND name != ''"
    )
    known_contacts = {
        canonical_contact_id(str(c["id"])): c["name"]
        for c in contacts
        if c["id"]
    }

    # Step 4: Process each session
    svc = MemoryService(ctx)
    llm = LLMDispatchService(ctx)

    bulletins_generated = 0
    errors = []

    for i, session in enumerate(sessions):
        session_key = session["session_key"]
        msg_count = session["message_count"]

        if msg_count < 3:
            continue

        logger.info(
            "Processing session %d/%d: %s (%d messages)",
            i + 1, len(sessions), session_key, msg_count,
        )

        messages = await db.fetch_all(
            "SELECT role, content, sender_id, created_at "
            "FROM session_messages "
            "WHERE session_key = ? AND role IN ('user', 'assistant') "
            "ORDER BY created_at ASC",
            (session_key,),
        )

        if not messages:
            continue

        participants_set: set[str] = set()

        for msg in messages:
            content = (msg["content"] or "").strip()
            if not content:
                continue

            sender = msg.get("sender_id", "")
            if sender:
                participants_set.add(sender)

        # Split by idle gaps (>15 min)
        msg_list = [
            m for m in messages
            if (m["content"] or "").strip()
        ]
        if not msg_list:
            continue

        idle_threshold = 15 * 60
        segments: list[list[dict]] = []
        current_seg: list[dict] = [msg_list[0]]

        for j in range(1, len(msg_list)):
            prev_t = msg_list[j - 1]["created_at"]
            curr_t = msg_list[j]["created_at"]
            try:
                gap = (datetime.fromisoformat(curr_t) - datetime.fromisoformat(prev_t)).total_seconds()
            except (ValueError, TypeError):
                gap = 0
            if gap > idle_threshold and current_seg:
                segments.append(current_seg)
                current_seg = [msg_list[j]]
            else:
                current_seg.append(msg_list[j])
        if current_seg:
            segments.append(current_seg)

        participants = [
            {"id": canonical_contact_id(cid), "name": known_contacts.get(canonical_contact_id(cid), cid)}
            for cid in participants_set
        ]

        channel_id = resolve_channel_id(session_key)
        visibility = derive_visibility(session_key)

        for seg_idx, seg_msgs in enumerate(segments):
            seg_messages = [
                {
                    "sender_contact_id": m.get("sender_id", "assistant"),
                    "timestamp": m.get("created_at", ""),
                    "content": (m.get("content") or "")[:500],
                }
                for m in seg_msgs
            ]

            if sum(len(m["content"]) for m in seg_messages) < 50:
                continue

            if dry_run:
                logger.info(
                    "  Would generate bulletins for %s segment-%d (%d messages)",
                    session_key, seg_idx + 1, len(seg_msgs),
                )
                continue

            gen_input = build_generator_input(
                session_key=session_key,
                messages=seg_messages,
                participants=participants,
            )

            try:
                bulletin_texts = await generate_bulletins(llm, gen_input)
                first_msg_ts = seg_msgs[0].get("created_at", "") if seg_msgs else None
                last_msg_ts = seg_msgs[-1].get("created_at", "") if seg_msgs else None
                for text in bulletin_texts:
                    await svc.write_bulletin(
                        workspace_dir,
                        channel_id=channel_id,
                        source_type="seed",
                        source_id=session_key,
                        content=text,
                        visibility=visibility,
                        occurred_at=last_msg_ts,
                        session_range_start=first_msg_ts or "",
                        session_range_end=last_msg_ts or "",
                    )
                    bulletins_generated += 1

                if bulletin_texts:
                    logger.info("  Generated %d bulletin(s) for segment-%d", len(bulletin_texts), seg_idx + 1)

            except Exception as exc:
                logger.exception("Error generating bulletins for %s", session_key)
                errors.append({"session_key": session_key, "error": str(exc)})

        if dry_run:
            continue

    # Step 5: Seed manual bulletins from tool call logs
    manual_result: dict[str, Any] = {"status": "skipped"}
    if not dry_run:
        from cyborg_server.services.memory.seed_manual import seed_manual_bulletins
        logger.info("Seeding manual bulletins from tool call logs...")
        manual_result = await seed_manual_bulletins(ctx, workspace_dir)
        logger.info("Manual seed: %s", manual_result)

    # Step 6: Run dream pipeline
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
        "errors": errors,
        "dream": dream_result,
    }
