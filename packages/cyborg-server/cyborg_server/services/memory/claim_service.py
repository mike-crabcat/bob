"""Claim service — extract, store, and manage atomic memory claims."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from cyborg_server.services.memory.models import Claim, Bulletin

logger = logging.getLogger(__name__)


async def write_claim(db: Any, claim: Claim) -> str:
    """Write a claim to the database."""
    await db.execute(
        "INSERT OR REPLACE INTO memory_claims "
        "(id, type, subject_id, predicate, object_id, status, "
        "source_bulletins, visibility, scope, created_at, superseded_by, body) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            claim.id,
            claim.type,
            claim.subject_id,
            claim.predicate,
            claim.object_id,
            claim.status,
            json.dumps(claim.source_bulletins),
            claim.visibility,
            json.dumps(claim.scope),
            claim.created_at.isoformat(),
            json.dumps(claim.superseded_by),
            claim.body,
        ),
    )
    logger.info("Claim written: %s", claim.id)
    return claim.id


async def read_claim(db: Any, claim_id: str) -> Claim | None:
    """Read a claim from the database."""
    row = await db.fetch_one(
        "SELECT * FROM memory_claims WHERE id = ?",
        (claim_id,),
    )
    if not row:
        return None
    return _row_to_claim(row)


async def get_active_claims(db: Any, entity_id: str) -> list[Claim]:
    """Get all active claims for a given entity (as subject or object)."""
    rows = await db.fetch_all(
        "SELECT * FROM memory_claims "
        "WHERE status = 'active' AND (subject_id = ? OR object_id = ?)",
        (entity_id, entity_id),
    )
    return [_row_to_claim(r) for r in rows]


async def get_all_claims(db: Any) -> list[Claim]:
    """Get all claims."""
    rows = await db.fetch_all("SELECT * FROM memory_claims")
    return [_row_to_claim(r) for r in rows]


def _row_to_claim(row: dict) -> Claim:
    """Convert a database row to a Claim dataclass."""
    return Claim(
        id=row["id"],
        type=row["type"],
        subject_id=row["subject_id"],
        predicate=row["predicate"],
        object_id=row["object_id"],
        status=row["status"],
        source_bulletins=json.loads(row["source_bulletins"]) if row["source_bulletins"] else [],
        visibility=row["visibility"],
        scope=json.loads(row["scope"]) if row["scope"] else [],
        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else datetime.now(),
        superseded_by=json.loads(row["superseded_by"]) if row["superseded_by"] else [],
        body=row["body"] or "",
    )


async def extract_claims_from_bulletin(
    llm: Any,
    bulletin: Bulletin,
    existing_claims: list[Claim] | None = None,
) -> list[Claim]:
    """Use LLM to extract atomic claims from a bulletin."""
    existing_context = ""
    if existing_claims:
        lines = [f"- {c.subject_id} {c.predicate} {c.object_id or ''} ({c.status})" for c in existing_claims[:50]]
        existing_context = "\n\n## Existing Claims\n\n" + "\n".join(lines)

    from cyborg_server.services.memory.models import serialize_frontmatter
    bulletin_text = serialize_frontmatter({
        "id": bulletin.id,
        "channel_id": bulletin.channel_id,
        "visibility": bulletin.visibility,
        "scope": bulletin.scope,
        "entities": bulletin.entities,
    }, bulletin.content)

    user_prompt = f"## Bulletin\n\n{bulletin_text}{existing_context}"

    response = await llm.chat(
        messages=[
            {"role": "system", "content": CLAIM_EXTRACTION_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        model=llm.memory_model,
        call_category="memory_claim_extraction",
        temperature=0.2,
        max_tokens=2000,
    )

    try:
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        items = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Claim extraction: failed to parse LLM response")
        return []

    if not isinstance(items, list):
        return []

    from cyborg_server.services.memory.entity_resolver import normalize_entity_id

    claims = []
    now = datetime.now()
    for item in items:
        if not isinstance(item, dict):
            continue
        claim = Claim(
            id=f"claim-{bulletin.id}-{len(claims) + 1:03d}",
            type=item.get("type", "fact"),
            subject_id=normalize_entity_id(item.get("subject_id", "")),
            predicate=item.get("predicate", ""),
            object_id=normalize_entity_id(item["object_id"]) if isinstance(item.get("object_id"), str) else item.get("object_id"),
            status="active",
            source_bulletins=[bulletin.id],
            visibility=bulletin.visibility,
            scope=bulletin.scope,
            created_at=now,
            superseded_by=[],
            body=item.get("body", ""),
        )
        claims.append(claim)

    return claims


from cyborg_server.services.memory.prompts import CLAIM_EXTRACTION_PROMPT
