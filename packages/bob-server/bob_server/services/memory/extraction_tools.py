"""Tools for the silent-turn memory extractor.

A narrow, write-oriented tool subset for the "is there anything worth
remembering?" idle turn. Read access reuses the shared entity tools; write
access (create_entity / add_claim) is provenance-threaded: every claim
created during a turn records the turn's session_message id in
`source_messages`, so claims trace back to the exact turn that extracted them.

Deliberately omitted vs. reconciliation: retract / supersede / delete / merge.
Extraction is additive — it records what was said, not repairs existing state.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from bob_server.services.memory.claim_types import ENTITY_REF_CLAIM_KEYS
from bob_server.services.memory.claim_service import write_claim
from bob_server.services.memory.entity_tools import (
    make_get_entity_tool,
    make_list_entities_tool,
)
from bob_server.services.memory.models import Claim
from bob_server.services.tools import Tool, tool

logger = logging.getLogger(__name__)


def make_extraction_tools(db: Any, turn_message_id: str) -> list[Tool]:
    """Create the silent-turn extractor's tool subset.

    All claims written through these tools carry ``turn_message_id`` in their
    ``source_messages`` provenance, linking them to the synthetic assistant
    message produced by this turn.
    """

    list_entities = make_list_entities_tool(db)
    get_entity = make_get_entity_tool(db)

    @tool
    async def add_claim(
        subject_id: str,
        claim_type_key: str,
        value: str = "",
        object_id: str = "",
    ) -> str:
        """Record a single fact/preference/etc. about an entity that already exists.

        Use `value` for scalar data (dates, free text); use `object_id` to reference
        another entity (a connection, a member, a leg). Set only one of them.
        Always check the entity first with get_entity to avoid recording a duplicate
        of something already known.
        """
        if not subject_id or not claim_type_key:
            return "Error: subject_id and claim_type_key are required."
        val = value if value else None
        obj = object_id if object_id else None
        if claim_type_key in ENTITY_REF_CLAIM_KEYS and val and not obj:
            obj, val = val, None
        if val and obj:
            if claim_type_key in ENTITY_REF_CLAIM_KEYS:
                val = None
            else:
                obj = None
        claim = Claim(
            id=f"claim-extr-{uuid.uuid4().hex[:8]}",
            claim_type_key=claim_type_key,
            subject_id=subject_id,
            value=val,
            object_id=obj,
            status="active",
            source_messages=[turn_message_id],
            created_at=datetime.now(),
        )
        await write_claim(db, claim)
        return f"Recorded {claim_type_key} on {subject_id}" + (f" → {obj}" if obj else f" = {val}")

    @tool
    async def create_entity(
        entity_id: str,
        entity_type: str,
        claims_json: str = "[]",
    ) -> str:
        """Create a new entity (person, trip, group, etc.) and optionally add claims.

        Use this only when get_entity / list_entities confirm the entity does not yet
        exist. `claims_json` is a JSON array of objects with `claim_type_key` and
        either `value` or `object_id`. All claims are attributed to this turn.
        """
        if not entity_id or not entity_type:
            return "Error: entity_id and entity_type are required."
        existing = await db.fetch_one(
            "SELECT entity_id FROM memory_entities WHERE entity_id = ? AND status = 'active'",
            (entity_id,),
        )
        if existing:
            return f"Entity {entity_id} already exists — use add_claim on it instead."
        display_name = entity_id.split("-", 1)[-1].replace("-", " ").title() if "-" in entity_id else entity_id
        await db.execute(
            "INSERT OR IGNORE INTO memory_entities (entity_id, entity_type, display_name, status) "
            "VALUES (?, ?, ?, 'active')",
            (entity_id, entity_type, display_name),
        )
        try:
            new_claims = json.loads(claims_json) if claims_json else []
        except json.JSONDecodeError:
            return f"Created entity {entity_id} but claims_json was invalid."
        for cl in new_claims:
            claim = Claim(
                id=f"claim-extr-{uuid.uuid4().hex[:8]}",
                claim_type_key=cl.get("claim_type_key", ""),
                subject_id=entity_id,
                value=cl.get("value"),
                object_id=cl.get("object_id"),
                status="active",
                source_messages=[turn_message_id],
                created_at=datetime.now(),
            )
            await write_claim(db, claim)
        return f"Created entity {entity_id} ({entity_type}) with {len(new_claims)} claims"

    return [list_entities, get_entity, create_entity, add_claim]
