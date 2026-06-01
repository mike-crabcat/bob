"""Claim service — extract, store, and manage atomic memory claims."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from cyborg_server.services.memory.models import Claim, Bulletin, parse_frontmatter, serialize_frontmatter
from cyborg_server.services.memory.prompts import CLAIM_EXTRACTION_PROMPT
from cyborg_server.services.memory.entity_resolver import normalize_entity_id

logger = logging.getLogger(__name__)


def claim_path(memory_dir: Path, claim_id: str) -> Path:
    return memory_dir / "claims" / f"{claim_id}.md"


def write_claim(memory_dir: Path, claim: Claim) -> str:
    """Write a claim to disk."""
    claims_dir = memory_dir / "claims"
    claims_dir.mkdir(parents=True, exist_ok=True)

    fm = {
        "id": claim.id,
        "type": claim.type,
        "subject_id": claim.subject_id,
        "predicate": claim.predicate,
        "object_id": claim.object_id,
        "status": claim.status,
        "source_bulletins": claim.source_bulletins,
        "visibility": claim.visibility,
        "scope": claim.scope,
        "created_at": claim.created_at.isoformat(),
        "superseded_by": claim.superseded_by,
    }

    body = f"# Claim\n\n{claim.body}"
    path = claim_path(memory_dir, claim.id)
    path.write_text(serialize_frontmatter(fm, body), encoding="utf-8")
    return str(path)


def read_claim(memory_dir: Path, claim_id: str) -> Claim | None:
    """Read a claim from disk."""
    path = claim_path(memory_dir, claim_id)
    if not path.is_file():
        return None
    fm, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    if not fm:
        return None
    return Claim(
        id=fm.get("id", claim_id),
        type=fm.get("type", "fact"),
        subject_id=fm.get("subject_id", ""),
        predicate=fm.get("predicate", ""),
        object_id=fm.get("object_id"),
        status=fm.get("status", "active"),
        source_bulletins=fm.get("source_bulletins", []),
        visibility=fm.get("visibility", "private"),
        scope=fm.get("scope", []),
        created_at=datetime.fromisoformat(fm["created_at"]) if "created_at" in fm else datetime.now(),
        superseded_by=fm.get("superseded_by", []),
        body=body.strip().removeprefix("# Claim").strip(),
    )


def get_active_claims(memory_dir: Path, entity_id: str) -> list[Claim]:
    """Get all active claims for a given entity (as subject or object)."""
    claims_dir = memory_dir / "claims"
    if not claims_dir.is_dir():
        return []

    results = []
    for md_file in claims_dir.glob("*.md"):
        claim = read_claim(memory_dir, md_file.stem)
        if claim and claim.status == "active":
            if claim.subject_id == entity_id or claim.object_id == entity_id:
                results.append(claim)
    return results


def get_all_claims(memory_dir: Path) -> list[Claim]:
    """Get all claims."""
    claims_dir = memory_dir / "claims"
    if not claims_dir.is_dir():
        return []

    results = []
    for md_file in claims_dir.glob("*.md"):
        claim = read_claim(memory_dir, md_file.stem)
        if claim:
            results.append(claim)
    return results


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

    claims = []
    now = datetime.now()
    for item in items:
        if not isinstance(item, dict):
            continue
        claim = Claim(
            id=f"claim-{bulletin.created_at.strftime('%Y-%m-%d')}-{len(claims) + 1:03d}",
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
