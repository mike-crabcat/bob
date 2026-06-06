"""Memory tools for LLM function calling (v7 claim-centric).

Usage:
    tools.extend(make_memory_tools(ctx, session_key=session_key))
"""

from __future__ import annotations

import json
import logging

from cyborg_server.context import AppContext
from cyborg_server.services.memory import MemoryService
from cyborg_server.services.memory.channels import resolve_channel_id
from cyborg_server.services.memory.claim_types import render_entity
from cyborg_server.services.memory.claim_service import get_active_claims
from cyborg_server.services.tools import Tool, tool

logger = logging.getLogger(__name__)


def make_memory_tools(ctx: AppContext, *, session_key: str) -> list[Tool]:
    """Create memory recall/find/note tools bound to the given context."""

    svc = MemoryService(ctx)

    @tool
    async def recall(query: str) -> str:
        """Retrieve entity information by ID, name, or natural language query.
        Returns the entity's claims rendered as readable text."""
        from cyborg_server.services.memory.tools import recall as _recall
        return await _recall(ctx.db, query)

    @tool
    async def find(
        entity_type: str,
        claim_type_key: str = "",
        value: str = "",
    ) -> str:
        """Find entities by type with optional claim filters.
        Entity types: contact, group, location, trip, tripstop, transport, event, task, artifact, decision.
        Returns matching entity IDs and display names."""
        from cyborg_server.services.memory.tools import find as _find
        return await _find(ctx.db, entity_type, claim_type_key or None, value or None)

    @tool
    async def note(
        text: str,
        context_entity_id: str = "",
    ) -> str:
        """Accept new information from conversation. Queues as a bulletin for digestion.
        Optionally link to a context entity ID (e.g. trip-bali-2026)."""
        from cyborg_server.services.memory.tools import note as _note
        channel_id = resolve_channel_id(session_key)
        return await _note(ctx.db, text, context_entity_id or None, channel_id=channel_id)

    @tool
    async def memory_write(
        content: str,
        channel_id: str = "",
        visibility: str = "private",
    ) -> str:
        """Create a memory bulletin. Content is markdown.
        Queued for digestion into claims. Use note() for simpler input."""
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
    async def memory_read(entity_id: str) -> str:
        """Read a specific memory entity by ID. Returns rendered claims."""
        workspace = ctx.settings.harness.workspace_dir

        entity = await svc.read_entity(workspace, entity_id)
        if entity is None:
            return json.dumps({"error": f"Entity not found: {entity_id}"})

        # Fetch and render claims
        claims = await get_active_claims(ctx.db, entity_id)
        claim_dicts = [
            {"claim_type_key": c.claim_type_key, "object_id": c.object_id, "value": c.value}
            for c in claims
        ]
        rendered = render_entity(entity.entity_type, entity.display_name, claim_dicts)
        return json.dumps({
            "entity_id": entity.entity_id,
            "entity_type": entity.entity_type,
            "display_name": entity.display_name,
            "rendered": rendered,
        })

    return [recall, find, note, memory_write, memory_read]
