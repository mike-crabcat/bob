"""Entity reconciliation — LLM-driven consistency checking and repair.

After dream processes bulletins, this module reviews entities against
per-type rules, applies deterministic fixes, and raises questions for
ambiguous conflicts.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from cyborg_server.services.memory.claim_types import (
    ENTITY_REF_CLAIM_KEYS,
    render_entity,
)
from cyborg_server.services.memory.claim_service import (
    write_claim,
    supersede_claim,
)
from cyborg_server.services.memory.models import Claim

logger = logging.getLogger(__name__)

_ENTITY_TYPE_PREFIXES = (
    "person-", "group-", "location-", "trip-", "tripstop-",
    "transport-", "event-", "task-", "file-", "thing-", "decision-",
)

# ---------------------------------------------------------------------------
# Per-entity-type reconciliation rules
# ---------------------------------------------------------------------------

RECONCILIATION_RULES: dict[str, str] = {
    "trip": (
        "1. Tripstop date ranges must not overlap.\n"
        "2. Each distinct stay (different location or non-contiguous dates at the "
        "same city) MUST be its own separate tripstop entity.\n"
        "3. If two tripstops reference the same location with contiguous/overlapping "
        "dates, they should be merged into one tripstop.\n"
        "4. The trip should have at least one tripstop.\n"
        "5. The trip should NOT have destination, start_date, or end_date claims — "
        "those are derived from the tripstops.\n"
        "6. When you create_entity or delete_entity for a tripstop, you MUST also "
        "update this trip's stop claims: retract stop claims referencing deleted "
        "tripstops, add stop claims referencing newly created tripstops.\n"
        "7. All currently referenced tripstops in the stop claims must actually exist "
        "as active entities. If a stop claim references a non-existent or archived "
        "tripstop, retract that claim.\n"
        "8. If 'Possibly Related Transports' are listed below, these are transport "
        "entities in the trip's date range that aren't linked to any tripstop. Link "
        "them by adding transport_to or transport_from claims to the appropriate "
        "tripstop (match by departure/arrival location vs tripstop stay location).\n"
        "9. If a tripstop has a transport_from link, its departure date can be derived "
        "from the transport's departure_time. If it has a transport_to link, its "
        "arrival date can be derived from when that transport arrives. Add missing "
        "arrival/departure claims when the transport data provides them."
    ),
    "tripstop": (
        "1. Arrival must be before departure.\n"
        "2. transport_to arrival_location should differ from the stay location's "
        "parent city (unless it's a local move).\n"
        "3. transport_from departure_location should match the stay location."
    ),
    "transport": (
        "1. Departure and arrival locations should differ.\n"
        "2. departure_time should be set.\n"
        "3. duration should be reasonable for the transport_type."
    ),
    "person": (
        "1. A person MUST NOT have a parent, child, or partner claim that references themselves.\n"
        "2. If a person has both parent and partner claims to the same entity, "
        "the parent claim is likely wrong — retract it.\n"
        "3. Semantically duplicate claims (same fact worded differently, e.g. "
        "'coeliac/GF' and 'coeliac-safe options') should be retracted in favor of "
        "the most specific/sourced version.\n"
        "4. Inferred claims with no source bulletin are less reliable than "
        "bulletin-grounded claims. If they conflict, prefer the sourced claim."
    ),
    "group": "No specific reconciliation rules.",
    "location": "No specific reconciliation rules.",
    "event": (
        "1. start_time must be before end_time.\n"
        "2. If associated_trip is set, the event should fall within the trip date range."
    ),
    "task": "No specific reconciliation rules.",
    "file": "No specific reconciliation rules.",
    "thing": "No specific reconciliation rules.",
    "decision": "No specific reconciliation rules.",
}

# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

RECONCILIATION_PROMPT = """\
You are a Memory Reconciliation Agent. You review an entity and its related \
sub-entities, checking for inconsistencies against a set of rules.

## Entity Under Review

{entity_view}

## Source Bulletins (ground truth reference material)

{bulletins}

## Answered Questions (ground truth from the user)

{answers}

## Reconciliation Rules for {entity_type}

{rules}

## Your Task

1. Check the entity data against each rule above.
2. Use the Source Bulletins and claim provenance to resolve conflicts — claims with \
no source bulletin are inferred and less reliable than claims grounded in bulletins.
3. If you can determine a clear fix, propose a claim operation — prefer acting over asking.
4. Answered questions are ground truth from the user — act on them directly, do not re-ask.
5. If a child entity has been split or merged, update the parent's composition claims (e.g. trip.stop) accordingly.
6. If a tripstop has no stay, arrival, or departure claims and its split replacements exist, archive it and update the parent trip's stop claims.
7. If the fix is truly ambiguous with no answered question guiding it, raise a question instead.
8. If the entity is consistent, return empty arrays.

## Output Format

Return a JSON object:

```json
{{
  "issues": [
    {{"rule": "which rule is violated", "detail": "what specifically is wrong"}}
  ],
  "operations": [
    {{
      "action": "supersede",
      "subject_id": "entity-id",
      "claim_type_key": "claim_key",
      "old_value": "current value",
      "new_value": "corrected value",
      "reason": "why"
    }},
    {{
      "action": "add",
      "claim_type_key": "claim_key",
      "subject_id": "entity-id",
      "value": "the value",
      "reason": "why"
    }},
    {{
      "action": "retract",
      "subject_id": "entity-id",
      "claim_type_key": "claim_key",
      "old_value": "value to remove",
      "reason": "why"
    }},
    {{
      "action": "create_entity",
      "entity_id": "new-entity-id",
      "entity_type": "tripstop",
      "claims": [
        {{"claim_type_key": "stay", "value": "location-id"}},
        {{"claim_type_key": "arrival", "value": "2026-07-01T15:00"}}
      ],
      "reason": "why"
    }},
    {{
      "action": "delete_entity",
      "entity_id": "entity-id-to-archive",
      "reason": "why"
    }}
  ],
  "questions": [
    {{
      "entity_id": "entity-id-in-question",
      "question": "What should X be?",
      "options": ["Option A", "Option B"],
      "context": "Two tripstops overlap but may be intentional"
    }}
  ]
}}
```

Return ONLY the JSON object. No other text.
If no issues found: {{"issues": [], "operations": [], "questions": []}}
"""


# ---------------------------------------------------------------------------
# render_entity_full — recursive entity view assembly
# ---------------------------------------------------------------------------

async def render_entity_full(
    db: Any,
    entity_id: str,
    depth: int = 2,
    _visited: set[str] | None = None,
) -> str:
    """Render an entity with its related sub-entities expanded recursively."""
    if _visited is None:
        _visited = set()
    if entity_id in _visited:
        return f"[Cycle: {entity_id}]"
    _visited = _visited | {entity_id}

    entity_row = await db.fetch_one(
        "SELECT entity_id, entity_type, display_name FROM memory_entities "
        "WHERE entity_id = ? AND status = 'active'",
        (entity_id,),
    )
    if not entity_row:
        return f"[Unknown entity: {entity_id}]"

    claims = await db.fetch_all(
        "SELECT claim_type_key, object_id, value, source_bulletins FROM memory_claims "
        "WHERE status = 'active' AND subject_id = ?",
        (entity_id,),
    )
    claim_dicts = [
        {"claim_type_key": r["claim_type_key"], "object_id": r["object_id"], "value": r["value"]}
        for r in claims
    ]

    rendered = render_entity(
        entity_row["entity_type"],
        entity_row["display_name"],
        claim_dicts,
    )

    # Append provenance tags (source bulletins) to each claim line
    provenance_lines: list[str] = []
    for r in claims:
        val = r["value"] or r["object_id"] or ""
        src = r["source_bulletins"] or ""
        src_label = ""
        if src:
            try:
                bids = json.loads(src) if isinstance(src, str) else src
                if bids:
                    src_label = f"  [source: {', '.join(bids)}]"
                else:
                    src_label = "  [source: none — inferred]"
            except (json.JSONDecodeError, TypeError):
                src_label = f"  [source: {src}]"
        elif r["claim_type_key"] not in ("truth",):
            src_label = "  [source: none — inferred]"
        provenance_lines.append(f"  {r['claim_type_key']}: {val}{src_label}")
    provenance_block = "Claim provenance for {}:\n".format(entity_id) + "\n".join(provenance_lines)
    rendered += "\n\n" + provenance_block

    # Skip expanding into person/group/file entities — noise for reconciliation
    _SKIP_EXPAND_PREFIXES = ("person-", "group-", "file-")

    if depth > 0:
        seen_refs: set[str] = set()
        for claim in claim_dicts:
            key = claim["claim_type_key"]
            if key not in ENTITY_REF_CLAIM_KEYS:
                continue
            ref_id = claim.get("object_id") or claim.get("value") or ""
            if not ref_id or not ref_id.startswith(_ENTITY_TYPE_PREFIXES):
                continue
            if ref_id.startswith(_SKIP_EXPAND_PREFIXES):
                continue
            if ref_id in seen_refs:
                continue
            seen_refs.add(ref_id)
            child = await render_entity_full(db, ref_id, depth - 1, _visited)
            rendered += f"\n\n--- {key}: {ref_id} ---\n{child}"

    return rendered


async def _find_orphan_transports(db: Any, trip_id: str) -> str:
    """Find transport entities in the trip's date range that aren't linked
    to any tripstop via transport_to/transport_from. These are likely related
    to the trip and should be shown to the LLM for linking.
    """
    # Get the trip's date range from tripstop arrival/departure claims
    stop_claims = await db.fetch_all(
        "SELECT mc.object_id FROM memory_claims mc "
        "WHERE mc.subject_id = ? AND mc.claim_type_key = 'stop' AND mc.status = 'active'",
        (trip_id,),
    )
    tripstop_ids = [c["object_id"] for c in stop_claims if c["object_id"]]
    if not tripstop_ids:
        return ""

    # Get all arrival/departure values for these tripstops
    placeholders = ",".join("?" for _ in tripstop_ids)
    date_claims = await db.fetch_all(
        f"SELECT subject_id, claim_type_key, value FROM memory_claims "
        f"WHERE subject_id IN ({placeholders}) AND claim_type_key IN ('arrival', 'departure') "
        f"AND status = 'active' AND value IS NOT NULL",
        tuple(tripstop_ids),
    )

    # Parse date range
    dates: list[str] = []
    for c in date_claims:
        v = (c["value"] or "")[:10]  # Take just the date portion
        if v and v[0].isdigit():
            dates.append(v)
    if not dates:
        return ""

    dates.sort()
    range_start = dates[0]
    range_end = dates[-1]

    # Find already-linked transport IDs
    linked_transport_claims = await db.fetch_all(
        f"SELECT object_id, value FROM memory_claims "
        f"WHERE subject_id IN ({placeholders}) AND claim_type_key IN ('transport_to', 'transport_from') "
        f"AND status = 'active'",
        tuple(tripstop_ids),
    )
    linked_transports: set[str] = set()
    for c in linked_transport_claims:
        if c["object_id"]:
            linked_transports.add(c["object_id"])
        if c["value"] and c["value"].startswith("transport-"):
            linked_transports.add(c["value"])

    # Find transport entities with departure_time in the trip's date range
    transports = await db.fetch_all(
        "SELECT e.entity_id, e.display_name FROM memory_entities e "
        "JOIN memory_claims mc ON mc.subject_id = e.entity_id "
        "WHERE e.entity_type = 'transport' AND e.status = 'active' "
        "AND mc.claim_type_key = 'departure_time' AND mc.status = 'active' "
        "AND mc.value IS NOT NULL AND mc.value >= ? AND mc.value <= ?",
        (range_start, range_end + "\xff"),
    )

    orphan_transports = [t for t in transports if t["entity_id"] not in linked_transports]
    if not orphan_transports:
        return ""

    # Render each orphan transport
    sections: list[str] = ["\n\n--- Possibly Related Transports (in trip date range, not linked to any tripstop) ---"]
    for t in orphan_transports:
        rendered = await render_entity_full(db, t["entity_id"], depth=1)
        sections.append(f"\n{t['entity_id']}:\n{rendered}")

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# reconcile_entity — LLM-driven detect + fix + question
# ---------------------------------------------------------------------------

async def _load_answers(db: Any, entity_id: str) -> str:
    """Load answered questions for an entity as ground-truth context."""
    rows = await db.fetch_all(
        "SELECT question, answer FROM memory_questions "
        "WHERE entity_id = ? AND status = 'answered' AND answer IS NOT NULL",
        (entity_id,),
    )
    if not rows:
        return "(none)"
    lines = [f"Q: {r['question']}\nA: {r['answer']}" for r in rows]
    return "\n".join(lines)


async def _write_questions(
    db: Any, entity_id: str, questions: list[dict],
) -> list[str]:
    """Write unresolved questions to the memory_questions table."""
    ids = []
    now = datetime.now().isoformat()
    for q in questions:
        qid = f"question-{uuid.uuid4().hex[:8]}"
        await db.execute(
            "INSERT INTO memory_questions "
            "(id, entity_id, question, options, context, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'open', ?)",
            (
                qid,
                entity_id,  # Always use the reconciliation root, not a leaf
                q.get("question", ""),
                json.dumps(q.get("options", [])),
                q.get("context", ""),
                now,
            ),
        )
        ids.append(qid)
        logger.info("Reconciliation question raised: %s", qid)
    return ids


async def _apply_operations(
    db: Any, entity_id: str, operations: list[dict],
) -> list[dict]:
    """Apply reconciliation operations (supersede/add/delete_entity)."""
    applied = []

    for op in operations:
        action = op.get("action")

        if action == "supersede":
            subject = op.get("subject_id", "")
            ctk = op.get("claim_type_key", "")
            old_val = op.get("old_value", "")
            new_val = op.get("new_value", "")

            if not subject or not ctk or not new_val:
                logger.warning("Skipping incomplete supersede op: %s", op)
                continue

            # Find the matching active claim
            rows = await db.fetch_all(
                "SELECT id, value, object_id FROM memory_claims "
                "WHERE status = 'active' AND subject_id = ? AND claim_type_key = ?",
                (subject, ctk),
            )
            for row in rows:
                val = row["value"] or row["object_id"] or ""
                if old_val and val != old_val:
                    continue
                recon_ref = f"recon-{entity_id}"
                new_claim = Claim(
                    id=f"claim-recon-{uuid.uuid4().hex[:8]}",
                    claim_type_key=ctk,
                    subject_id=subject,
                    value=new_val,
                    status="active",
                    source_bulletins=[],
                    created_at=datetime.now(),
                )
                await supersede_claim(db, row["id"], new_claim, recon_ref)
                applied.append(op)
                break

        elif action == "add":
            subject = op.get("subject_id", "")
            ctk = op.get("claim_type_key", "")
            val = op.get("value")
            obj = op.get("object_id")
            if not subject or not ctk:
                logger.warning("Skipping incomplete add op: %s", op)
                continue
            # Entity-ref claims should use object_id, not value
            if ctk in ENTITY_REF_CLAIM_KEYS and val and not obj:
                obj, val = val, None
            claim = Claim(
                id=f"claim-recon-{uuid.uuid4().hex[:8]}",
                claim_type_key=ctk,
                subject_id=subject,
                value=val,
                object_id=obj,
                status="active",
                source_bulletins=[],
                created_at=datetime.now(),
            )
            await write_claim(db, claim)
            applied.append(op)

        elif action == "retract":
            subject = op.get("subject_id", "")
            ctk = op.get("claim_type_key", "")
            old_val = op.get("old_value", "")
            if not subject or not ctk:
                logger.warning("Skipping incomplete retract op: %s", op)
                continue
            rows = await db.fetch_all(
                "SELECT id, value, object_id FROM memory_claims "
                "WHERE status = 'active' AND subject_id = ? AND claim_type_key = ?",
                (subject, ctk),
            )
            recon_ref = f"recon-{entity_id}"
            for row in rows:
                val = row["value"] or row["object_id"] or ""
                if old_val and val != old_val:
                    continue
                await db.execute(
                    "UPDATE memory_claims SET status = 'superseded', superseded_by = ? WHERE id = ?",
                    (json.dumps([recon_ref]), row["id"]),
                )
                applied.append(op)
                if not old_val:
                    break  # If no old_value specified, only retract first match

        elif action == "create_entity":
            target_id = op.get("entity_id", "")
            target_type = op.get("entity_type", "")
            new_claims = op.get("claims", [])
            if not target_id or not target_type:
                logger.warning("Skipping incomplete create_entity op: %s", op)
                continue
            # Create entity record
            display_name = target_id.split("-", 1)[-1].replace("-", " ").title() if "-" in target_id else target_id
            await db.execute(
                "INSERT OR IGNORE INTO memory_entities (entity_id, entity_type, display_name, status) "
                "VALUES (?, ?, ?, 'active')",
                (target_id, target_type, display_name),
            )
            # Write initial claims
            for cl in new_claims:
                claim = Claim(
                    id=f"claim-recon-{uuid.uuid4().hex[:8]}",
                    claim_type_key=cl.get("claim_type_key", ""),
                    subject_id=target_id,
                    value=cl.get("value"),
                    object_id=cl.get("object_id"),
                    status="active",
                    source_bulletins=[],
                    created_at=datetime.now(),
                )
                await write_claim(db, claim)
            applied.append(op)

        elif action == "delete_entity":
            target_id = op.get("entity_id", "")
            if not target_id:
                continue
            await db.execute(
                "UPDATE memory_entities SET status = 'archived' WHERE entity_id = ?",
                (target_id,),
            )
            await db.execute(
                "UPDATE memory_claims SET status = 'superseded' "
                "WHERE subject_id = ? AND status = 'active'",
                (target_id,),
            )
            applied.append(op)

    return applied


async def _collect_bulletin_text(db: Any, entity_id: str) -> str:
    """Collect source bulletin text for all claims on an entity and its children.

    Gathers bulletin IDs from claim source_bulletins, loads the bulletin content,
    and returns a formatted block for the reconciliation prompt.
    """
    # Collect from direct claims
    claim_rows = await db.fetch_all(
        "SELECT source_bulletins FROM memory_claims "
        "WHERE status = 'active' AND subject_id = ? AND source_bulletins IS NOT NULL",
        (entity_id,),
    )
    bulletin_ids: set[str] = set()
    for r in claim_rows:
        try:
            bids = json.loads(r["source_bulletins"]) if r["source_bulletins"] else []
            bulletin_ids.update(bids)
        except (json.JSONDecodeError, TypeError):
            pass

    # Also from direct entity-bulletin links
    eb_rows = await db.fetch_all(
        "SELECT bulletin_id FROM memory_entity_bulletins WHERE entity_id = ?",
        (entity_id,),
    )
    bulletin_ids.update(r["bulletin_id"] for r in eb_rows)

    if not bulletin_ids:
        return "(no source bulletins available)"

    # Load bulletin content
    placeholders = ",".join("?" for _ in bulletin_ids)
    rows = await db.fetch_all(
        f"SELECT id, content FROM memory_bulletins WHERE id IN ({placeholders})",
        tuple(bulletin_ids),
    )
    if not rows:
        return "(no source bulletins available)"

    lines = []
    for r in rows:
        content = r["content"] or ""
        # Truncate long bulletins to avoid bloating the prompt
        if len(content) > 500:
            content = content[:500] + "..."
        lines.append(f"[{r['id']}] {content}")
    return "\n\n".join(lines)


async def reconcile_entity(
    db: Any,
    llm: Any,
    entity_id: str,
    *,
    update_fts_fn=None,
) -> dict[str, Any]:
    """Run reconciliation for a single entity.

    Returns {"issues": [...], "operations_applied": [...], "questions_raised": [...]}.
    """
    entity_row = await db.fetch_one(
        "SELECT entity_type FROM memory_entities WHERE entity_id = ? AND status = 'active'",
        (entity_id,),
    )
    if not entity_row:
        return {"issues": [], "operations_applied": [], "questions_raised": []}

    entity_type = entity_row["entity_type"]
    rules = RECONCILIATION_RULES.get(entity_type, "No specific reconciliation rules.")

    entity_view = await render_entity_full(db, entity_id)

    # For trips: find orphan transports in the trip's date range
    # and append them to the entity view so the LLM can link them to tripstops
    if entity_type == "trip":
        entity_view += await _find_orphan_transports(db, entity_id)

    answers = await _load_answers(db, entity_id)

    # Collect source bulletin text for provenance
    bulletin_text = await _collect_bulletin_text(db, entity_id)

    system_prompt = RECONCILIATION_PROMPT.format(
        entity_view=entity_view,
        bulletins=bulletin_text,
        answers=answers,
        entity_type=entity_type,
        rules=rules,
    )

    response = await llm.chat(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Review this entity for consistency issues."},
        ],
        model=llm.memory_model,
        call_category="memory_reconciliation",
        temperature=0.1,
        max_tokens=1500,
    )

    try:
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Reconciliation: failed to parse LLM response for %s", entity_id)
        return {"issues": [], "operations_applied": [], "questions_raised": []}

    issues = result.get("issues", [])
    operations = result.get("operations", [])
    questions = result.get("questions", [])

    applied = await _apply_operations(db, entity_id, operations)
    question_ids = await _write_questions(db, entity_id, questions)

    # Re-render FTS for all affected entities
    if update_fts_fn and (applied or question_ids):
        touched = {entity_id}
        for op in applied:
            touched.add(op.get("subject_id", ""))
            if op.get("entity_id"):
                touched.add(op["entity_id"])
        for eid in touched:
            try:
                await update_fts_fn(eid)
            except Exception:
                pass

    if issues:
        logger.info(
            "Reconciliation for %s: %d issues, %d ops applied, %d questions",
            entity_id, len(issues), len(applied), len(question_ids),
        )

    return {
        "issues": issues,
        "operations_applied": applied,
        "questions_raised": question_ids,
    }
