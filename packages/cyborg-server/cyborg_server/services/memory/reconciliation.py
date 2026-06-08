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
    ENTITY_TYPE_PREFIXES,
    ENTITY_TYPE_REGISTRY,
    render_entity,
)
from cyborg_server.services.memory.claim_service import (
    write_claim,
    supersede_claim,
    _is_valid_file_path,
)
from cyborg_server.services.memory.models import Claim

logger = logging.getLogger(__name__)

_ENTITY_TYPE_PREFIXES = ENTITY_TYPE_PREFIXES

# ---------------------------------------------------------------------------
# Per-entity-type reconciliation rules (sourced from ENTITY_TYPE_REGISTRY)
# ---------------------------------------------------------------------------

_SKIP_EXPAND_PREFIXES = tuple(et.prefix for et in ENTITY_TYPE_REGISTRY.values() if et.skip_expand)

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
5. If a child entity has been split or merged, update the parent's composition claims (e.g. trip.leg) accordingly.
6. When splitting a stay into multiple new stays, you MUST include a delete_entity operation \
for the original stay AFTER creating its replacements. The original stay is no longer valid once \
its data has been distributed to the new entities. Also retract the parent's leg claim pointing to it.
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
      "entity_type": "stay",
      "claims": [
        {{"claim_type_key": "accommodation", "value": "location-id"}},
        {{"claim_type_key": "arrival_date", "value": "2026-07-01T15:00"}}
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
      "context": "Two stays overlap but may be intentional"
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
        entity_id=entity_id,
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

    # Skip expanding into types marked skip_expand — noise for reconciliation
    skip_prefixes = _SKIP_EXPAND_PREFIXES

    if depth > 0:
        seen_refs: set[str] = set()
        for claim in claim_dicts:
            key = claim["claim_type_key"]
            if key not in ENTITY_REF_CLAIM_KEYS:
                continue
            ref_id = claim.get("object_id") or claim.get("value") or ""
            if not ref_id or not ref_id.startswith(_ENTITY_TYPE_PREFIXES):
                continue
            if ref_id.startswith(skip_prefixes):
                continue
            if ref_id in seen_refs:
                continue
            seen_refs.add(ref_id)
            child = await render_entity_full(db, ref_id, depth - 1, _visited)
            rendered += f"\n\n--- {key}: {ref_id} ---\n{child}"

    return rendered



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


async def deprecate_file_entities_without_path(db: Any) -> list[str]:
    """Find active file entities with no valid file_path claim and deprecate them.

    Returns list of deprecated entity IDs.
    """
    file_entities = await db.fetch_all(
        "SELECT entity_id FROM memory_entities "
        "WHERE entity_type = 'file' AND status = 'active'"
    )
    if not file_entities:
        return []

    deprecated: list[str] = []
    for row in file_entities:
        eid = row["entity_id"]
        path_rows = await db.fetch_all(
            "SELECT value FROM memory_claims "
            "WHERE subject_id = ? AND claim_type_key = 'file_path' AND status = 'active'",
            (eid,),
        )
        has_valid_path = any(
            r["value"] and _is_valid_file_path(r["value"])
            for r in path_rows
        )
        if not has_valid_path:
            await db.execute(
                "UPDATE memory_entities SET status = 'deprecated' WHERE entity_id = ?",
                (eid,),
            )
            await db.execute(
                "UPDATE memory_claims SET status = 'superseded' "
                "WHERE subject_id = ? AND status = 'active'",
                (eid,),
            )
            deprecated.append(eid)
            logger.info("Deprecated file entity (no valid file_path): %s", eid)

    return deprecated


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
    et_def = ENTITY_TYPE_REGISTRY.get(entity_type)
    rules = et_def.reconciliation_rules if et_def else "No specific reconciliation rules."

    entity_view = await render_entity_full(db, entity_id)

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
