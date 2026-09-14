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
import re
import uuid
from datetime import datetime
from typing import Any

from server.services.memory.claim_types import ENTITY_REF_CLAIM_KEYS
from server.services.memory.claim_service import validate_claim_for_write, write_claim
from server.services.memory.entity_tools import (
    make_get_entity_tool,
    make_list_entities_tool,
)
from server.services.memory.models import Claim
from server.services.tools import Tool, tool

logger = logging.getLogger(__name__)


def _name_tokens(name: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (name or "").lower()))


def _fuzzy_name_match(new_name: str, existing_name: str) -> bool:
    """Token-overlap sameness test for write-time soft resolution.

    The same real-world thing named with drift ("Nuffy Talkback Segment" vs
    "Nuffy Talkback AFL") shares its meaningful tokens; two dated entities
    ("… Cartoons 2026 09 12" vs "… 2026 09 13") differ exactly in their date
    tokens and must stay separate.
    """
    a, b = _name_tokens(new_name), _name_tokens(existing_name)
    digits_a = {t for t in a if t.isdigit()}
    digits_b = {t for t in b if t.isdigit()}
    if digits_a and digits_b and digits_a != digits_b:
        return False  # both dated, different dates → different things
    wa = {t for t in a if len(t) >= 3}
    wb = {t for t in b if len(t) >= 3}
    if not wa or not wb:
        return False
    inter = wa & wb
    return len(inter) >= 2 and len(inter) / min(len(wa), len(wb)) >= 0.6


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
        # Pre-check: subject must exist. write_claim enforces this too, but
        # surfacing the error here lets the LLM recover by calling
        # create_entity within the same turn instead of recording a claim
        # that recall/find can never reach.
        existing = await db.fetch_one(
            "SELECT 1 FROM memory_entities WHERE entity_id = ?",
            (subject_id,),
        )
        if not existing:
            return (
                f"Error: subject_id {subject_id!r} does not exist. "
                f"Call create_entity(entity_id={subject_id!r}, entity_type=...) first."
            )
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
        err = validate_claim_for_write(claim)
        if err:
            return f"Error: {err}"
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
        # Bob Events §2.0 layer 2 — write-time soft resolution: an exact or
        # fuzzy display-name match on the same entity type is almost certainly
        # the same real-world thing mentioned in another conversation. Steer
        # the extractor to reuse the existing id rather than minting a
        # near-duplicate that fragments routing (reconciliation merges what
        # still slips past). Exact match first; then token-overlap, which
        # catches naming drift ("Nuffy Talkback Segment" vs "Nuffy Talkback
        # AFL") while keeping differently-dated entities apart. A genuinely
        # different thing deserves a more specific slug — which then won't
        # display-name-match.
        rows = await db.fetch_all(
            "SELECT entity_id, display_name FROM memory_entities "
            "WHERE status = 'active' AND entity_type = ? AND entity_id != ?",
            (entity_type, entity_id),
        )
        near = None
        for row in rows:
            if (row["display_name"] or "").lower() == display_name.lower():
                near = row["entity_id"]
                break
        if near is None:
            for row in rows:
                if _fuzzy_name_match(display_name, row["display_name"] or ""):
                    near = row["entity_id"]
                    break
        if near:
            return (
                f"An existing {entity_type} entity '{near}' has the same or a very "
                f"similar display name to {entity_id!r}. If the conversation refers to "
                f"the same real-world thing, reuse it — call add_claim on {near} "
                "instead of creating a near-duplicate. Only create a new entity if it "
                "is genuinely a different thing (then pick a more distinguishing id)."
            )
        await db.execute(
            "INSERT OR IGNORE INTO memory_entities (entity_id, entity_type, display_name, status) "
            "VALUES (?, ?, ?, 'active')",
            (entity_id, entity_type, display_name),
        )
        try:
            new_claims = json.loads(claims_json) if claims_json else []
        except json.JSONDecodeError:
            return f"Created entity {entity_id} but claims_json was invalid JSON."
        if isinstance(new_claims, dict):
            return (
                f"Created entity {entity_id}, but claims_json must be a JSON ARRAY of "
                "claim objects (e.g. [{\"claim_type_key\": \"name\", \"value\": \"...\"}]), "
                "not a single object. Use add_claim to add each property."
            )
        if not isinstance(new_claims, list):
            return f"Created entity {entity_id} but claims_json was not a JSON array."
        written = 0
        for cl in new_claims:
            if not isinstance(cl, dict):
                continue
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
            err = validate_claim_for_write(claim)
            if err:
                # Entity is already created; report but keep going so the
                # remaining claims in the batch can still land.
                logger.warning("Skipped invalid claim on %s: %s", entity_id, err)
                continue
            await write_claim(db, claim)
            written += 1
        return f"Created entity {entity_id} ({entity_type}) with {written} claims"

    return [list_entities, get_entity, create_entity, add_claim]
