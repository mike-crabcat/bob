"""Seed manual bulletins — extract memory_write tool calls from LLM call logs."""

from __future__ import annotations

import json
import logging
from typing import Any
from pathlib import Path

logger = logging.getLogger(__name__)


async def seed_manual_bulletins(
    ctx: Any,
    workspace_dir: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Extract and replay memory_write tool calls from LLM call logs."""
    from cyborg_server.services.memory.service import MemoryService
    from cyborg_server.services.memory.channels import resolve_channel_id, derive_visibility

    db = ctx.db
    svc = MemoryService(ctx)

    rows = await db.fetch_all(
        """SELECT id, session_key, created_at, messages_json
           FROM llm_call_log
           WHERE messages_json LIKE '%"function_call"%'
             AND messages_json LIKE '%"memory_write"%'
           ORDER BY created_at ASC"""
    )

    if not rows:
        logger.info("No memory_write tool calls found in LLM logs")
        return {"status": "empty", "bulletins_generated": 0}

    logger.info("Found %d log rows containing memory_write calls", len(rows))

    bulletins_generated = 0
    errors = []
    seen_content: set[str] = set()

    # Deduplicate against existing bulletins
    existing = await db.fetch_all("SELECT content FROM memory_bulletins")
    for row in existing:
        seen_content.add(row["content"].strip())

    for row in rows:
        session_key = row["session_key"] or ""
        occurred_at = row["created_at"]

        try:
            messages = json.loads(row["messages_json"]) if row["messages_json"] else []
        except (json.JSONDecodeError, TypeError):
            continue

        for item in messages:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "function_call" or item.get("name") != "memory_write":
                continue

            try:
                args = json.loads(item.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                continue

            content = (args.get("content") or "").strip()
            if not content or content in seen_content:
                continue

            channel_id = args.get("channel_id", "")
            visibility = args.get("visibility", "private")

            if not channel_id and session_key:
                channel_id = resolve_channel_id(session_key)
            if not visibility or visibility == "private":
                visibility = derive_visibility(session_key) if session_key else "private"

            if dry_run:
                logger.info("  Would write manual bulletin: %s", content[:80])
                continue

            try:
                await svc.write_bulletin(
                    workspace_dir,
                    channel_id=channel_id,
                    source_type="manual",
                    source_id=session_key,
                    content=content,
                    visibility=visibility,
                    occurred_at=occurred_at,
                )
                seen_content.add(content)
                bulletins_generated += 1
            except Exception as exc:
                logger.exception("Error writing manual bulletin from log %s", row["id"])
                errors.append({"log_id": row["id"], "error": str(exc)})

    logger.info("Generated %d manual bulletins", bulletins_generated)

    return {
        "status": "completed",
        "log_rows_scanned": len(rows),
        "bulletins_generated": bulletins_generated,
        "errors": errors,
    }
