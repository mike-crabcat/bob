"""Claim service — extract, store, and manage atomic memory claims."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from bob_server.services.memory.claim_types import (
    get_all_keys,
    build_extraction_prompt_section,
    ENTITY_TYPE_REGISTRY,
)
from bob_server.services.memory.models import Claim, Bulletin
from bob_server.services.memory.prompts import build_extraction_prompt

logger = logging.getLogger(__name__)

_NEW_PERSON_RE = re.compile(r"^person:new:(.+)$")
_NEW_PERSON_ALT_RE = re.compile(r"^person-new[:\-](.+)$")


async def write_claim(db: Any, claim: Claim) -> str:
    """Write a claim to the database. Deduplicates by merging bulletin sources."""
    row = await db.fetch_one(
        "SELECT 1 FROM memory_claim_types WHERE key = ?",
        (claim.claim_type_key,),
    )
    if not row:
        logger.warning("Skipping claim %s: unknown claim_type_key %r", claim.id, claim.claim_type_key)
        return claim.id

    # Deduplicate: if writing an active claim with the same content as an existing
    # active claim, merge bulletin sources instead of creating a duplicate.
    if claim.status == "active":
        existing = await db.fetch_one(
            "SELECT id, source_bulletins FROM memory_claims "
            "WHERE status = 'active' AND claim_type_key = ? AND subject_id = ? "
            "AND COALESCE(object_id, '') = COALESCE(?, '') "
            "AND COALESCE(value, '') = COALESCE(?, '')",
            (claim.claim_type_key, claim.subject_id, claim.object_id, claim.value),
        )
        if existing:
            existing_id = existing["id"]
            existing_bullets: list[str] = json.loads(existing["source_bulletins"]) if existing["source_bulletins"] else []
            merged = list(dict.fromkeys(existing_bullets + claim.source_bulletins))
            if len(merged) > len(existing_bullets):
                await db.execute(
                    "UPDATE memory_claims SET source_bulletins = ? WHERE id = ?",
                    (json.dumps(merged), existing_id),
                )
            return existing_id

    await db.execute(
        "INSERT OR REPLACE INTO memory_claims "
        "(id, claim_type_key, subject_id, object_id, value, status, "
        "source_bulletins, visibility, scope, created_at, superseded_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            claim.id,
            claim.claim_type_key,
            claim.subject_id,
            claim.object_id,
            claim.value,
            claim.status,
            json.dumps(claim.source_bulletins),
            claim.visibility,
            json.dumps(claim.scope),
            claim.created_at.isoformat(),
            json.dumps(claim.superseded_by),
        ),
    )
    logger.info("Claim written: %s", claim.id)
    return claim.id


async def supersede_claim(
    db: Any,
    old_claim_id: str,
    new_claim: Claim,
    superseded_by_ref: str,
) -> str:
    """Mark old claim as superseded and write a replacement claim."""
    await db.execute(
        "UPDATE memory_claims SET status = 'superseded', superseded_by = ? WHERE id = ?",
        (json.dumps([superseded_by_ref]), old_claim_id),
    )
    return await write_claim(db, new_claim)


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


async def get_claims_by_type(
    db: Any, entity_id: str, claim_type_key: str
) -> list[Claim]:
    """Get active claims for an entity filtered by claim type."""
    rows = await db.fetch_all(
        "SELECT * FROM memory_claims "
        "WHERE status = 'active' AND subject_id = ? AND claim_type_key = ?",
        (entity_id, claim_type_key),
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
        claim_type_key=row["claim_type_key"],
        subject_id=row["subject_id"],
        object_id=row["object_id"],
        value=row["value"],
        status=row["status"],
        source_bulletins=json.loads(row["source_bulletins"]) if row["source_bulletins"] else [],
        visibility=row["visibility"],
        scope=json.loads(row["scope"]) if row["scope"] else [],
        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else datetime.now(),
        superseded_by=json.loads(row["superseded_by"]) if row["superseded_by"] else [],
    )


def _name_to_slug(name: str) -> str:
    """Convert a person name to a slug: lowercase, hyphens, alphanumeric only."""
    return re.sub(r"[^a-z0-9\-]", "", name.strip().lower().replace(" ", "-"))


# Values that are never valid as file_path — too vague to locate a real file
_INVALID_PATH_VALUES: frozenset[str] = frozenset({
    "", "workspace", "project", "root", "project root", "workspace root",
    "home", "directory", "folder", "repo", "repository", "local",
    "file system", "filesystem", "desktop", "documents", "downloads",
})

_URL_PREFIXES = ("https://", "http://", "s3://", "gs://")


def _is_valid_file_path(path: str) -> bool:
    """Return True if the file_path looks like a real workspace path or URL."""
    stripped = path.strip().strip("\"'").lower()
    if stripped in _INVALID_PATH_VALUES:
        return False
    if any(stripped.startswith(p) for p in _URL_PREFIXES):
        return True
    # Reject bare "." and ".."
    if stripped in (".", ".."):
        return False
    # Workspace-relative paths must contain at least one path separator or
    # a file extension dot, and must not be just a bare directory name.
    if "/" in stripped or "\\" in stripped:
        return True
    # Dotfiles like ".env" — starts with dot and has chars after
    if stripped.startswith(".") and len(stripped) > 1:
        return True
    # Bare filename with extension: must have at least one char before the dot
    if "." in stripped:
        base = stripped.rsplit(".", 1)[0]
        return len(base) > 0
    return False


def _invalid_file_entities(
    missing_path_ids: set[str],
    path_values: dict[str, str],
) -> set[str]:
    """Return subject IDs for file entities with missing or invalid file_path."""
    invalid = set(missing_path_ids)  # No file_path at all
    for sid, path_val in path_values.items():
        if not _is_valid_file_path(path_val):
            invalid.add(sid)
    return invalid


async def extract_claims_from_bulletin(
    llm: Any,
    bulletin: Bulletin,
    entity_types_in_bulletin: list[str] | None = None,
    existing_claims: list[Claim] | None = None,
    known_group_entity_id: str | None = None,
    contact_roster: str = "",
    group_members: str = "",
    db: Any = None,
    premapped_content: str | None = None,
    bot_name: str = "Bob",
) -> list[Claim]:
    """Use LLM to extract atomic claims from a bulletin."""
    if entity_types_in_bulletin:
        claim_types_section = build_extraction_prompt_section(entity_types_in_bulletin)
    else:
        claim_types_section = build_extraction_prompt_section(["person", "trip", "event", "location"])

    system_prompt = build_extraction_prompt(claim_types_section, bot_name=bot_name)

    existing_context = ""
    if existing_claims:
        lines = [
            f"- {c.subject_id} [{c.claim_type_key}] {c.object_id or c.value or ''} ({c.status})"
            for c in existing_claims[:50]
        ]
        existing_context = "\n\n## Existing Claims\n\n" + "\n".join(lines)

    known_entities_section = ""
    if db is not None:
        entity_rows = await db.fetch_all(
            "SELECT entity_id, entity_type, display_name FROM memory_entities WHERE status = 'active'"
        )
        if entity_rows:
            lines = [f"- {r['entity_id']} ({r['entity_type']}) {r['display_name']}" for r in entity_rows]
            known_entities_section = "\n\n## Known Entities\n\n" + "\n".join(lines)

    group_hint = ""
    if known_group_entity_id:
        group_hint = (
            f"\n\n## Group Context\n\n"
            f"This bulletin originates from a group session. "
            f"Use `subject_id: {known_group_entity_id}` for any claims about the group itself."
        )

    roster_section = ""
    if contact_roster:
        roster_section = "\n\n## Known Persons\n\n" + contact_roster
    if group_members:
        roster_section += "\n\n## Group Members\n\n" + group_members

    content_for_extraction = premapped_content or bulletin.content
    bulletin_text = f"[Bulletin: {bulletin.id}]\nChannel: {bulletin.channel_id}\nVisibility: {bulletin.visibility}\n\n{content_for_extraction}"

    user_prompt = f"## Bulletin\n\n{bulletin_text}{roster_section}{known_entities_section}{existing_context}{group_hint}"

    response = await llm.chat(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        model=llm.memory_model,
        call_category="memory_claim_extraction",
        temperature=0.2,
        max_tokens=4000,
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

    from bob_server.services.memory.entity_resolver import normalize_entity_id

    valid_keys = get_all_keys()

    claims = []
    file_subject_ids: set[str] = set()
    file_path_values: dict[str, str] = {}  # subject_id -> file_path value

    for item in items:
        if not isinstance(item, dict):
            continue

        raw_key = item.get("claim_type_key", "")
        if raw_key not in valid_keys:
            logger.warning("Unknown claim type key: %s, skipping", raw_key)
            continue

        raw_object = item.get("object_id")
        raw_value = item.get("value")

        if isinstance(raw_object, str):
            raw_object = normalize_entity_id(raw_object)

        subject_id = normalize_entity_id(item.get("subject_id", ""))

        # Track file subjects for file_path validation
        if subject_id.startswith("file-"):
            file_subject_ids.add(subject_id)
            if raw_key == "file_path" and raw_value:
                file_subject_ids.discard(subject_id)
                file_path_values[subject_id] = raw_value

        claim = Claim(
            id=f"claim-{bulletin.id}-{len(claims) + 1:03d}",
            claim_type_key=raw_key,
            subject_id=subject_id,
            object_id=raw_object if raw_object else None,
            value=raw_value if raw_value else None,
            status="active",
            source_bulletins=[bulletin.id],
            visibility=bulletin.visibility,
            created_at=bulletin.created_at,
            superseded_by=[],
        )
        claims.append(claim)

    # Drop claims for file entities that have no file_path or invalid file_path
    invalid_file_ids = _invalid_file_entities(file_subject_ids, file_path_values)
    if invalid_file_ids:
        logger.warning("Dropping file entities without valid file_path: %s", invalid_file_ids)
        claims = [c for c in claims if c.subject_id not in invalid_file_ids]

    # Normalize entity IDs (colon-based, :new: prefixes, double prefixes)
    _normalize_entity_ids(claims)

    # Resolve person:new:{Name} markers to slug-based person IDs
    _resolve_new_persons(claims)

    # Drop claims with null subject_id (set by _resolve_new_persons for non-person names)
    claims = [c for c in claims if c.subject_id is not None]

    # Enforce: exactly one of object_id or value must be set (not both, not neither)
    valid_claims = []
    for c in claims:
        has_object = c.object_id is not None and c.object_id != ""
        has_value = c.value is not None and c.value != ""
        if has_object and has_value:
            # Prefer value, drop object_id
            c.object_id = None
            valid_claims.append(c)
        elif has_object or has_value:
            valid_claims.append(c)
        else:
            logger.warning("Dropping claim with no object_id and no value: %s", c.id)
    claims = valid_claims

    return claims


_ENTITY_TYPE_PREFIXES = tuple(ENTITY_TYPE_REGISTRY.keys())
_ENTITY_COLON_RE = re.compile(r"^(" + "|".join(_ENTITY_TYPE_PREFIXES) + r"):(.+)$")


def _normalize_one_entity_id(val: str) -> str:
    """Fix colon-separated entity IDs to use hyphens: file:foo -> file-foo."""
    # Fix double prefixes: person-person-xxx -> person-xxx
    for prefix in _ENTITY_TYPE_PREFIXES:
        double = f"{prefix}-{prefix}-"
        if val.startswith(double):
            new_id = val[len(prefix) + 1:]
            logger.info("Fixing double prefix: %s -> %s", val, new_id)
            return new_id

    m = _ENTITY_COLON_RE.match(val)
    if m:
        prefix = m.group(1)
        rest = m.group(2).strip()
        if prefix == "person":
            if rest.startswith("new:"):
                return val  # Let _resolve_new_persons handle person:new:Name
            slug = _name_to_slug(rest)
            new_id = f"person-{slug}"
            logger.info("Normalizing entity ID: %s -> %s", val, new_id)
            return new_id
        rest = rest.lower().replace(" ", "-")
        if rest.startswith("new:"):
            rest = rest[4:]
        rest = re.sub(r"[^a-z0-9\-]", "", rest)
        new_id = f"{prefix}-{rest}"
        logger.info("Normalizing entity ID: %s -> %s", val, new_id)
        return new_id
    return val


def _normalize_entity_ids(claims: list[Claim]) -> None:
    """Normalize non-canonical entity IDs (colon-based, :new: prefixes, double prefixes)."""
    for claim in claims:
        for attr in ("subject_id", "object_id"):
            val = getattr(claim, attr)
            if not isinstance(val, str):
                continue
            normalized = _normalize_one_entity_id(val)
            if normalized != val:
                setattr(claim, attr, normalized)


_NON_PERSON_WORDS = frozenset({
    "subagent", "bot", "assistant", "agent", "ai", "claude", "gpt", "llm", "bob",
    "system", "tool", "service", "whatsapp", "telegram", "slack", "email", "sms",
    "api", "server", "client", "workflow", "pipeline", "instructions", "changelog",
    "upcoming", "folder", "skills", "generated", "jingle", "openclaw", "google",
    "photos", "image", "pdf", "spreadsheet", "document", "file", "protocol",
    "reseller", "outreach", "mood", "presence", "support", "thread", "script",
    "voice", "cloning", "wrapper", "cronjob", "workspace", "memory", "rebuild",
    "caller", "recipient", "proxy", "human", "unknown", "sender", "user",
    "someone", "skill", "call", "outbound", "phone", "system", "setup",
    "test", "fresh", "agents", "instructions", "generated-images",
})


def _looks_like_person(name: str) -> bool:
    """Heuristic check: does this name look like a real human person?"""
    name = name.strip()
    if not name or len(name) < 2:
        return False
    if not re.search(r"[a-zA-Z]", name):
        return False
    if any(c in name for c in ("/", "\\", "http", ".com", ".org", ".io", "_", "://")):
        return False
    if len(name) > 40:
        return False
    if len(name.split()) > 4:
        return False
    words = name.lower().split()
    if any(w in _NON_PERSON_WORDS for w in words):
        return False
    if re.match(r"^\+?\d{5,}$", name):
        return False
    if re.match(r"^.*@.*\.\w+$", name):
        return False
    if len(words) >= 3 and name == name.lower():
        return False
    return True


def _resolve_new_persons(claims: list[Claim]) -> None:
    """Resolve person:new:{Name} markers to slug-based person IDs."""
    for claim in claims:
        for attr in ("subject_id", "object_id"):
            val = getattr(claim, attr)
            if not isinstance(val, str) or not val.startswith(("person-", "person:")):
                continue

            m = _NEW_PERSON_RE.match(val)
            if not m:
                m = _NEW_PERSON_ALT_RE.match(val)
            if m:
                name = m.group(1).strip()
                if not _looks_like_person(name):
                    logger.warning("Skipping non-person name: %s", name)
                    setattr(claim, attr, None)
                    continue
                slug = _name_to_slug(name)
                new_id = f"person-{slug}"
                logger.info("Resolving new person: %s -> %s", val, new_id)
                setattr(claim, attr, new_id)
