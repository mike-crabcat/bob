"""Shared read-only entity tools for the memory subsystem.

`list_entities` and `get_entity` are used by both reconciliation and the
silent-turn extractor. They are pure functions of the db handle, so they
live here as factories that close over a connection and return `Tool`s.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from bob_server.services.memory.claim_types import ENTITY_TYPES, render_entity
from bob_server.services.tools import Tool, tool

logger = logging.getLogger(__name__)


def make_list_entities_tool(db: Any) -> Tool:
    """Return a `list_entities` tool bound to the given database connection."""

    @tool
    async def list_entities(entity_type: str) -> str:
        """List active entities of a given type. Returns entity IDs and display names.

        Use this to discover related entities (e.g. find all trips, all connections).
        """
        if entity_type not in ENTITY_TYPES:
            return f"Unknown entity type: {entity_type}. Valid types: {', '.join(ENTITY_TYPES)}"
        rows = await db.fetch_all(
            "SELECT entity_id, display_name FROM memory_entities "
            "WHERE entity_type = ? AND status = 'active'",
            (entity_type,),
        )
        if not rows:
            return f"No active {entity_type} entities found."
        lines = [f"{r['entity_id']} ({r['display_name'] or r['entity_id']})" for r in rows]
        return "\n".join(lines)

    return list_entities


def make_get_entity_tool(db: Any) -> Tool:
    """Return a `get_entity` tool bound to the given database connection."""

    @tool
    async def get_entity(entity_id: str) -> str:
        """Get full rendered details of an entity including all its claims.

        Returns the entity's type, display name, all claim values, and provenance.
        """
        row = await db.fetch_one(
            "SELECT entity_id, entity_type, display_name FROM memory_entities "
            "WHERE entity_id = ? AND status = 'active'",
            (entity_id,),
        )
        if not row:
            return f"Entity not found: {entity_id}"

        claims = await db.fetch_all(
            "SELECT claim_type_key, object_id, value, source_bulletins, source_messages "
            "FROM memory_claims WHERE status = 'active' AND subject_id = ?",
            (entity_id,),
        )
        claim_dicts = [
            {"claim_type_key": r["claim_type_key"], "object_id": r["object_id"], "value": r["value"]}
            for r in claims
        ]

        rendered = await render_entity(
            row["entity_type"], row["display_name"], claim_dicts,
            entity_id=entity_id, db=db,
        )

        # Append provenance
        prov_lines: list[str] = []
        for r in claims:
            val = r["value"] or r["object_id"] or ""
            src = r["source_bulletins"] or ""
            msgs = r["source_messages"] or ""
            src_label = ""
            try:
                bids = json.loads(src) if isinstance(src, str) else src
                mids = json.loads(msgs) if isinstance(msgs, str) else msgs
            except (json.JSONDecodeError, TypeError):
                bids, mids = [], []
            tags: list[str] = []
            if bids:
                tags.append(f"{len(bids)} bulletin{'s' if len(bids) != 1 else ''}")
            if mids:
                tags.append(f"{len(mids)} message{'s' if len(mids) != 1 else ''}")
            if tags:
                src_label = f"  [source: {', '.join(tags)}]"
            elif r["claim_type_key"] not in ("truth",):
                src_label = "  [source: none — inferred]"
            prov_lines.append(f"  {r['claim_type_key']}: {val}{src_label}")
        if prov_lines:
            rendered += "\n\nProvenance:\n" + "\n".join(prov_lines)

        # Also show reverse references (entities that reference this one)
        reverse = await db.fetch_all(
            "SELECT c.claim_type_key, c.subject_id, e.display_name "
            "FROM memory_claims c "
            "LEFT JOIN memory_entities e ON e.entity_id = c.subject_id "
            "WHERE c.status = 'active' AND c.object_id = ?",
            (entity_id,),
        )
        if reverse:
            rendered += "\n\nReferenced by:"
            for rc in reverse:
                label = rc["display_name"] or rc["subject_id"]
                rendered += f"\n  - {label} [{rc['claim_type_key']}]"

        return rendered

    return get_entity
