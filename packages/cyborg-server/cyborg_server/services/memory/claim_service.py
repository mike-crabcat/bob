"""Claim service — extract, store, and manage atomic memory claims."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from cyborg_server.services.memory.models import Claim, Bulletin

logger = logging.getLogger(__name__)

_NEW_CONTACT_RE = re.compile(r"^contact:new:(.+)$")
_NEW_CONTACT_ALT_RE = re.compile(r"^contact-new[:\-](.+)$")
_NON_CANONICAL_CONTACT_RE = re.compile(r"^contact-([a-z][a-z\-]{1,80})$")


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
    # Write claim↔bulletin join rows
    if claim.source_bulletins:
        await db.execute_many(
            "INSERT OR IGNORE INTO memory_claim_bulletins (claim_id, bulletin_id) VALUES (?, ?)",
            [(claim.id, bid) for bid in claim.source_bulletins],
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


async def _ensure_contact(db: Any, name: str, created_cache: dict[str, str]) -> str | None:
    """Create a name-only contact if needed. Returns canonical contact-{hex8} ID.

    Uses created_cache to deduplicate within a single extraction batch.
    """
    # Check batch cache first
    cached = created_cache.get(name.lower())
    if cached:
        return cached

    # Check if a contact with this name already exists
    existing = await db.fetch_one(
        "SELECT id FROM contacts WHERE name = ? AND deleted_at IS NULL LIMIT 1",
        (name,),
    )
    if existing:
        canonical = f"contact-{str(existing['id'])[:8]}"
        created_cache[name.lower()] = canonical
        return canonical

    contact_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO contacts (id, name, phone_number, created_at, updated_at) "
        "VALUES (?, ?, NULL, ?, ?)",
        (contact_id, name, now, now),
    )
    canonical = f"contact-{contact_id[:8]}"
    created_cache[name.lower()] = canonical
    return canonical


async def extract_claims_from_bulletin(
    llm: Any,
    bulletin: Bulletin,
    existing_claims: list[Claim] | None = None,
    known_group_entity_id: str | None = None,
    contact_roster: str = "",
    group_members: str = "",
    db: Any = None,
) -> list[Claim]:
    """Use LLM to extract atomic claims from a bulletin."""
    existing_context = ""
    if existing_claims:
        lines = [f"- {c.subject_id} {c.predicate} {c.object_id or ''} ({c.status})" for c in existing_claims[:50]]
        existing_context = "\n\n## Existing Claims\n\n" + "\n".join(lines)

    group_hint = ""
    if known_group_entity_id:
        group_hint = (
            f"\n\n## Group Context\n\n"
            f"This bulletin originates from a group session. "
            f"Use `subject_id: {known_group_entity_id}` for any claims about the group itself."
        )

    roster_section = ""
    if contact_roster:
        roster_section = "\n\n## Known Contacts\n\n" + contact_roster
    if group_members:
        roster_section += "\n\n## Group Members\n\n" + group_members

    bulletin_text = f"[Bulletin: {bulletin.id}]\nChannel: {bulletin.channel_id}\nVisibility: {bulletin.visibility}\n\n{bulletin.content}"

    user_prompt = f"## Bulletin\n\n{bulletin_text}{roster_section}{existing_context}{group_hint}"

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

    valid_types = {"fact", "preference", "constraint", "decision", "task",
                   "availability", "booking", "artifact", "relationship", "private_note"}

    claims = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw_type = item.get("type", "fact")
        claim = Claim(
            id=f"claim-{bulletin.id}-{len(claims) + 1:03d}",
            type=raw_type if raw_type in valid_types else "fact",
            subject_id=normalize_entity_id(item.get("subject_id", "")),
            predicate=item.get("predicate", ""),
            object_id=normalize_entity_id(item["object_id"]) if isinstance(item.get("object_id"), str) else item.get("object_id"),
            status="active",
            source_bulletins=[bulletin.id],
            visibility=bulletin.visibility,
            created_at=bulletin.created_at,
            superseded_by=[],
            body=item.get("body", ""),
        )
        claims.append(claim)

    # Resolve contact:new:{Name} markers to real contacts
    if db is not None:
        await _resolve_new_contacts(db, claims)

    return claims


async def _resolve_new_contacts(db: Any, claims: list[Claim]) -> None:
    """Resolve non-canonical contact IDs to real canonical IDs.

    Handles these LLM output patterns:
    - contact:new:Full Name  (correct format)
    - contact-new:Full Name  (alternate colon)
    - contact-slug-name      (LLM-invented slug, e.g. contact-max)

    Deduplicates contact creation via a batch cache.
    """
    created_cache: dict[str, str] = {}

    for claim in claims:
        for attr in ("subject_id", "object_id"):
            val = getattr(claim, attr)
            if not isinstance(val, str) or not val.startswith("contact-"):
                continue

            # Pattern 1: contact:new:Full Name (correct)
            m = _NEW_CONTACT_RE.match(val)
            if m:
                name = m.group(1).strip()
                canonical_id = await _ensure_contact(db, name, created_cache)
                if canonical_id:
                    setattr(claim, attr, canonical_id)
                continue

            # Pattern 2: contact-new:Full Name (alternate colon/dash)
            m = _NEW_CONTACT_ALT_RE.match(val)
            if m:
                name = m.group(1).strip()
                canonical_id = await _ensure_contact(db, name, created_cache)
                if canonical_id:
                    setattr(claim, attr, canonical_id)
                continue

            # Pattern 3: contact-slug-name (non-canonical, LLM-invented)
            # Only if it doesn't look like a canonical hex8 ID
            m = _NON_CANONICAL_CONTACT_RE.match(val)
            if m:
                # Convert slug to display name: contact-max-parry -> Max Parry
                slug = m.group(1)
                name = " ".join(part.capitalize() for part in slug.split("-"))
                canonical_id = await _ensure_contact(db, name, created_cache)
                if canonical_id:
                    setattr(claim, attr, canonical_id)


from cyborg_server.services.memory.prompts import CLAIM_EXTRACTION_PROMPT
