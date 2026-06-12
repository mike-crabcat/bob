"""Entity resolver — map names and references to canonical entity IDs."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


def canonical_contact_id(uuid_str: str) -> str:
    """Convert a Contacts Database UUID to a canonical contact ID.

    >>> canonical_contact_id("7c9f0fd7-6134-4495-aa8c-f04f11bc15e8")
    'contact-7c9f0fd7'
    """
    return f"contact-{uuid_str[:8]}"


_CONTACT_REF_RE = re.compile(r"\{\{contact:([a-f0-9-]+)\|(.+?)\}\}")


async def resolve_contact(
    db: Any,
    name_or_ref: str,
) -> str | None:
    """Resolve a name or {{contact:UUID|Name}} reference to a canonical contact ID.

    Returns None if the contact cannot be resolved.
    """
    # Try {{contact:UUID|Name}} pattern
    match = _CONTACT_REF_RE.search(name_or_ref)
    if match:
        return canonical_contact_id(match.group(1))

    # Try plain UUID
    if re.match(r"^[a-f0-9]{8}-", name_or_ref):
        return canonical_contact_id(name_or_ref)

    # Try database lookup by name
    if db is not None:
        try:
            row = await db.fetch_one(
                "SELECT id FROM contacts WHERE display_name = ? OR name = ? LIMIT 1",
                (name_or_ref, name_or_ref),
            )
            if row:
                return canonical_contact_id(str(row["id"]))
        except Exception:
            pass

    return None


def resolve_contact_id_only(contact_id: str) -> str:
    """Convert any contact ID format to canonical form.

    Handles:
        7c9f0fd7-6134-4495-aa8c-f04f11bc15e8 -> contact-7c9f0fd7
        contact-7c9f0fd7 -> contact-7c9f0fd7 (unchanged)
        contact-7c9f0fd7-6134-4495-aa8c-f04f11bc15e8 -> contact-7c9f0fd7
    """
    if contact_id.startswith("contact-"):
        rest = contact_id[len("contact-"):]
        if re.match(r"^[a-f0-9]{8}-", rest):
            return f"contact-{rest[:8]}"
        return contact_id
    return canonical_contact_id(contact_id)


def normalize_entity_id(entity_id: str, entity_type: str = "") -> str:
    """Normalize any entity ID to its canonical form.

    Handles slashes, and other common LLM-generated ID variations.
    Person entities use slug-based IDs (person-{slug}), not UUIDs.
    The contacts table still uses contact-{hex8} for message attribution.
    """
    entity_id = entity_id.replace("/", "-").replace("\\", "-")

    # Raw UUID that looks like a contact → contact-{hex8}
    # (for backward compat with contact references from message attribution)
    if re.match(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$", entity_id):
        return f"contact-{entity_id[:8]}"

    # contact-{full-uuid} → contact-{hex8}
    if entity_id.startswith("contact-"):
        return resolve_contact_id_only(entity_id)

    # person- IDs are slug-based, pass through
    return entity_id


def resolve_entity(aliases: dict[str, str], name: str) -> str | None:
    """Look up an entity ID by name from the aliases map.

    Args:
        aliases: dict mapping display names to entity IDs
        name: the name to resolve
    """
    return aliases.get(name) or aliases.get(name.lower())


def load_aliases(memory_dir: Path) -> dict[str, str]:
    """Load the aliases file from memory/aliases/aliases.yml."""
    aliases_path = memory_dir / "aliases" / "aliases.yml"
    if not aliases_path.is_file():
        return {}
    raw = yaml.safe_load(aliases_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return {}
    return raw


async def load_aliases_db(db: Any) -> dict[str, str]:
    """Load aliases from the memory_aliases table."""
    rows = await db.fetch_all("SELECT alias, entity_id FROM memory_aliases")
    return {r["alias"]: r["entity_id"] for r in rows}


def load_entity_map(memory_dir: Path) -> dict[str, dict[str, str]]:
    """Load the entity map index from memory/indexes/entity-map.yml.

    Returns dict mapping entity_id -> {"entity_type": ..., "display_name": ..., "path": ...}
    """
    map_path = memory_dir / "indexes" / "entity-map.yml"
    if not map_path.is_file():
        return {}
    raw = yaml.safe_load(map_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return {}
    return raw


async def load_entity_map_db(db: Any) -> dict[str, dict[str, str]]:
    """Load entity map from the memory_entities table."""
    rows = await db.fetch_all(
        "SELECT entity_id, entity_type, display_name FROM memory_entities"
    )
    return {
        r["entity_id"]: {
            "entity_type": r["entity_type"],
            "display_name": r["display_name"] or "",
        }
        for r in rows
    }
