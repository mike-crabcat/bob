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
    render_entity,
)
from bob_server.services.memory.models import Claim

logger = logging.getLogger(__name__)

_NEW_PERSON_RE = re.compile(r"^person:new:(.+)$")
_NEW_PERSON_ALT_RE = re.compile(r"^person-new[:\-](.+)$")


async def update_entity_mentions(db: Any, entity_ids: list[str],
                                 message_ids: list[str]) -> None:
    """Upsert entity↔conversation mention intervals (bob-events-plan §2.1).

    Every claim write path funnels through here so the index covers
    extraction, memory_correct, reconciliation, and answer-question alike.
    Message ids that don't resolve (an extraction turn's marker message is
    inserted only after the tool loop finishes) are skipped here and picked
    up by the post-turn refresh. Never raises: the index is an accelerator,
    not a gate.
    """
    if not entity_ids or not message_ids:
        return
    try:
        from bob_server.repositories.history import HistoryRepository
        rows = await HistoryRepository(db).messages_by_ids(message_ids)
        for row in rows or []:
            cid = row["conversation_id"]
            at = row["created_at"] or ""
            for eid in entity_ids:
                await db.execute(
                    """INSERT OR IGNORE INTO memory_entity_mentions
                       (entity_id, conversation_id, first_message_id,
                        last_message_id, first_at, last_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (eid, cid, row["id"], row["id"], at, at))
                # Widen the interval; first_* stays at the earliest ever seen.
                await db.execute(
                    """UPDATE memory_entity_mentions
                       SET last_message_id = ?, last_at = ?
                       WHERE entity_id = ? AND conversation_id = ?
                         AND (last_at < ? OR (last_at = ? AND last_message_id != ?))""",
                    (row["id"], at, eid, cid, at, at, row["id"]))
    except Exception:
        logger.warning("entity-mention index update failed", exc_info=True)


async def list_claim_provenance(db: Any) -> list[dict[str, Any]]:
    """(subject, object, source_messages) for every claim — the
    mentions-backfill CLI reads through here (memory tables stay owned by
    this package)."""
    rows = await db.fetch_all(
        "SELECT subject_id, object_id, source_messages FROM memory_claims")
    return [dict(r) for r in rows or []]


async def count_entity_mentions(db: Any) -> int:
    row = await db.fetch_one("SELECT COUNT(*) AS n FROM memory_entity_mentions")
    return int(row["n"]) if row else 0


async def _claim_mention_entities(claim: Claim) -> list[str]:
    ids = [claim.subject_id]
    if claim.object_id and _is_valid_object_id(claim.object_id):
        ids.append(claim.object_id)
    return list(dict.fromkeys(ids))


async def write_claim(db: Any, claim: Claim) -> str:
    """Write a claim to the database. Deduplicates by merging message provenance."""
    # The DB CHECK constraint allows at most one of object_id / value. If both
    # were supplied (e.g. by supersede_claim_tool echoing the same string into
    # both fields), prefer object_id when it is a well-formed entity reference;
    # otherwise fall back to value.
    if claim.object_id and claim.value:
        if _is_valid_object_id(claim.object_id):
            claim.value = None
        else:
            claim.object_id = None

    row = await db.fetch_one(
        "SELECT 1 FROM memory_claim_types WHERE key = ?",
        (claim.claim_type_key,),
    )
    if not row:
        logger.warning("Skipping claim %s: unknown claim_type_key %r", claim.id, claim.claim_type_key)
        return claim.id

    # Orphan guard: subject_id must reference an existing entity row. Without
    # this, add_claim against a never-created slug produces claims that are
    # invisible to recall/find (both query memory_entities first). The check
    # is row-existence only — any status — so legitimate flows like
    # memory_correct's truth-claim-on-archived-entity still work.
    subject_row = await db.fetch_one(
        "SELECT 1 FROM memory_entities WHERE entity_id = ?",
        (claim.subject_id,),
    )
    if not subject_row:
        logger.warning(
            "Skipping orphan claim %s: subject_id %r has no row in memory_entities "
            "(call create_entity before add_claim)",
            claim.id, claim.subject_id,
        )
        return claim.id

    # Deduplicate: if writing an active claim with the same content as an existing
    # active claim, merge message provenance instead of creating a duplicate.
    if claim.status == "active":
        existing = await db.fetch_one(
            "SELECT id, source_messages FROM memory_claims "
            "WHERE status = 'active' AND claim_type_key = ? AND subject_id = ? "
            "AND COALESCE(object_id, '') = COALESCE(?, '') "
            "AND COALESCE(value, '') = COALESCE(?, '')",
            (claim.claim_type_key, claim.subject_id, claim.object_id, claim.value),
        )
        if existing:
            existing_id = existing["id"]
            existing_messages: list[str] = json.loads(existing["source_messages"]) if existing.get("source_messages") else []
            merged_messages = list(dict.fromkeys(existing_messages + claim.source_messages))
            if len(merged_messages) > len(existing_messages):
                await db.execute(
                    "UPDATE memory_claims SET source_messages = ? WHERE id = ?",
                    (json.dumps(merged_messages), existing_id),
                )
            # The dedupe merge widened provenance with this write's messages —
            # the entity was discussed in their conversation too (plan §2.1).
            await update_entity_mentions(db, await _claim_mention_entities(claim),
                                         claim.source_messages)
            return existing_id

    await db.execute(
        "INSERT OR REPLACE INTO memory_claims "
        "(id, claim_type_key, subject_id, object_id, value, status, "
        "source_messages, visibility, scope, created_at, superseded_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            claim.id,
            claim.claim_type_key,
            claim.subject_id,
            claim.object_id,
            claim.value,
            claim.status,
            json.dumps(claim.source_messages),
            claim.visibility,
            json.dumps(claim.scope),
            claim.created_at.isoformat(),
            json.dumps(claim.superseded_by),
        ),
    )
    logger.info("Claim written: %s", claim.id)
    await update_entity_mentions(db, await _claim_mention_entities(claim),
                                 claim.source_messages)
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


def _is_valid_object_id(object_id: str) -> bool:
    """Return True if object_id looks like a valid entity reference (e.g. person-foo)."""
    if not object_id:
        return False
    return any(object_id.startswith(f"{prefix}-") for prefix in _ENTITY_TYPE_PREFIXES)


def validate_claim_for_write(claim: Claim) -> str | None:
    """Return an error message if the claim violates a hard type contract, else None.

    LLM-facing tools call this before ``write_claim`` to reject claims that are
    structurally wrong (not just stylistically weak). Soft / stylistic checks
    belong in the prompts, not here — this is for cases where the data is
    categorically incorrect and storing it would pollute the entity.
    """
    if claim.claim_type_key == "file_ref":
        obj = (claim.object_id or "").strip()
        if not obj:
            return (
                "file_ref requires object_id pointing to a file-* entity. "
                "Drop the claim or create the file entity first."
            )
        if obj.startswith("file-new:") or not obj.startswith("file-"):
            return (
                f"file_ref object_id {obj!r} is not a file-* entity. "
                "Resolve the file reference first or drop the claim."
            )
    return None


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


async def update_entity_fts(db: Any, entity_id: str) -> None:
    """Render entity claims via template and refresh FTS + embedding indexes.

    Standalone mirror of MemoryService._update_entity_fts. Call after any write
    that changes an entity's claims or row, so recall-by-natural-language
    (which falls through to FTS/embedding search) sees the update immediately
    instead of waiting for the next dream cycle.
    """
    entity_row = await db.fetch_one(
        "SELECT entity_id, entity_type, display_name FROM memory_entities WHERE entity_id = ?",
        (entity_id,),
    )
    if not entity_row:
        return

    claims = await db.fetch_all(
        "SELECT claim_type_key, object_id, value FROM memory_claims "
        "WHERE status = 'active' AND subject_id = ?",
        (entity_id,),
    )

    claim_dicts = [
        {"claim_type_key": r["claim_type_key"], "object_id": r["object_id"], "value": r["value"]}
        for r in claims
    ]

    rendered = await render_entity(
        entity_row["entity_type"],
        entity_row["display_name"],
        claim_dicts,
        entity_id=entity_id,
        db=db,
    )
    await db.execute(
        "DELETE FROM memory_entities_fts WHERE entity_id = ?",
        (entity_id,),
    )
    await db.execute(
        "INSERT INTO memory_entities_fts(entity_id, display_name, rendered_body) "
        "VALUES (?, ?, ?)",
        (entity_id, entity_row["display_name"], rendered),
    )

    try:
        from bob_server.services.memory.embedding import embed_text, upsert_embedding
        embedding = await embed_text(rendered)
        if embedding:
            await upsert_embedding(db, entity_id, embedding)
    except Exception:
        pass
