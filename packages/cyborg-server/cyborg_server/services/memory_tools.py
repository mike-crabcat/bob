"""Memory tools for LLM function calling.

Usage:
    tools.extend(make_memory_tools(ctx, session_key=session_key))
"""

from __future__ import annotations

import json
import logging
import time
from uuid import uuid4

from cyborg_server.context import AppContext
from cyborg_server.services.memory import MemoryService
from cyborg_server.services.memory.channels import resolve_channel_id
from cyborg_server.services.tools import Tool, tool

logger = logging.getLogger(__name__)


def make_memory_tools(ctx: AppContext, *, session_key: str) -> list[Tool]:
    """Create memory read/write/search/browse tools bound to the given context."""

    svc = MemoryService(ctx)

    @tool
    async def memory_write(
        content: str,
        channel_id: str = "",
        visibility: str = "private",
    ) -> str:
        """Create a memory bulletin. Content is markdown.
        Your write will be queued as a bulletin and curated into entities by the dream process.
        Use channel_id to associate with a conversation channel (e.g. channel-whatsapp-group-123).
        Visibility can be: private, contact, group, channel, public."""
        workspace = ctx.settings.harness.workspace_dir

        cid = channel_id or resolve_channel_id(session_key)

        bulletin_id = await svc.write_bulletin(
            workspace,
            channel_id=cid,
            source_type="manual",
            source_id=session_key,
            content=content,
            visibility=visibility,
        )
        return json.dumps({"ok": True, "bulletin_id": bulletin_id, "queued": True})

    @tool
    async def memory_read(
        entity_id: str,
    ) -> str:
        """Read a specific memory entity by ID (e.g. contact-7c9f0fd7, trip-bali-2026).
        Returns full markdown content of the entity document."""
        workspace = ctx.settings.harness.workspace_dir

        entity = await svc.read_entity(workspace, entity_id)
        if entity is None:
            return json.dumps({"error": f"Entity not found: {entity_id}"})

        from cyborg_server.services.memory.models import serialize_frontmatter
        return serialize_frontmatter(
            {
                "entity_id": entity.entity_id,
                "entity_type": entity.entity_type,
                "display_name": entity.display_name,
                **entity.extra_frontmatter,
            },
            entity.body,
        )

    @tool
    async def memory_search(
        query: str,
        entity_type: str = "",
    ) -> str:
        """Search across memory entities. Optionally filter to a specific entity type
        (contacts, groups, channels, trips, locations, events, tasks, artifacts, decisions).
        Returns an abstract summarizing findings, plus a list of matching entities
        with IDs and relevance explanations. Use memory_read with the entity_id to read the full document."""
        workspace = ctx.settings.harness.workspace_dir

        start = time.monotonic()
        result = await svc.search_entries(workspace, query, entity_type=entity_type)
        latency = time.monotonic() - start

        try:
            await ctx.db.execute(
                "INSERT INTO memory_search_log (id, query, results_json, session_key, result_count, latency_seconds) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (str(uuid4()), query, json.dumps(result), session_key, len(result.get("results", [])), latency),
            )
        except Exception:
            logger.debug("Failed to log memory search", exc_info=True)

        return json.dumps(result)

    @tool
    async def memory_browse(
        entity_type: str,
    ) -> str:
        """List all memory entities of a given type.
        Types: contacts, groups, channels, trips, locations, events, tasks, artifacts, decisions.
        Returns entity_id, display_name, and status for each."""
        workspace = ctx.settings.harness.workspace_dir

        entities = await svc.list_entities(workspace, entity_type)
        entries = [
            {
                "entity_id": e.entity_id,
                "display_name": e.display_name,
                "status": e.status,
            }
            for e in entities
        ]
        return json.dumps(entries)

    @tool
    async def memory_graph(
        entity_id: str,
        depth: int = 1,
    ) -> str:
        """Explore the memory graph around an entity.
        Returns the entity and its directly related entities (Related Entities section).
        Depth controls how many hops to follow (1 = immediate neighbors only).
        Currently only depth=1 is supported."""
        workspace = ctx.settings.harness.workspace_dir

        entity = await svc.read_entity(workspace, entity_id)
        if entity is None:
            return json.dumps({"error": f"Entity not found: {entity_id}"})

        related: dict[str, list[dict]] = {}
        for cat, ids in entity.related_entities.items():
            if not ids:
                continue
            cat_entries = []
            for rid in ids[:20]:
                related_entity = await svc.read_entity(workspace, rid)
                if related_entity:
                    cat_entries.append({
                        "entity_id": related_entity.entity_id,
                        "entity_type": related_entity.entity_type,
                        "display_name": related_entity.display_name,
                    })
                else:
                    cat_entries.append({"entity_id": rid, "status": "not_found"})
            if cat_entries:
                related[cat] = cat_entries

        return json.dumps({
            "entity_id": entity.entity_id,
            "entity_type": entity.entity_type,
            "display_name": entity.display_name,
            "related": related,
        })

    return [memory_write, memory_read, memory_search, memory_browse, memory_graph]
