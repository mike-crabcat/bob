"""Map non-canonical contact IDs to canonical contact-{hex8} IDs via the contacts DB."""

from __future__ import annotations

import re

from cyborg_server.services.memory.contact_directory import ContactDirectory

_CANONICAL_RE = re.compile(r"^contact-[a-f0-9]{8}$")


def is_canonical_contact_id(entity_id: str) -> bool:
    return bool(_CANONICAL_RE.match(entity_id))


def slug_to_display_name(entity_id: str) -> str:
    """Extract a display name from a non-canonical contact slug.

    "contact-blair-nicol" -> "Blair Nicol"
    "contact-blair" -> "Blair"
    "unresolved-contact-blair" -> "Blair"
    Returns "" for canonical or non-extractable IDs.
    """
    if is_canonical_contact_id(entity_id):
        return ""
    prefix = "unresolved-contact-" if entity_id.startswith("unresolved-contact-") else "contact-"
    slug = entity_id.removeprefix(prefix)
    if not slug or slug[0].isdigit():
        return ""
    return " ".join(part.capitalize() for part in slug.split("-"))


def reconcile_contact_id(
    entity_id: str,
    display_name: str,
    directory: ContactDirectory | None,
) -> str:
    """Return the canonical contact ID for *entity_id* if it can be resolved.

    If *entity_id* is already canonical, a non-contact entity, or cannot be
    matched against the contacts DB, it is returned unchanged.
    """
    if not entity_id:
        return entity_id
    if not entity_id.startswith("contact-") and not entity_id.startswith("unresolved-contact-"):
        return entity_id
    if _CANONICAL_RE.match(entity_id):
        return entity_id
    if directory is None:
        return entity_id

    name = display_name or slug_to_display_name(entity_id)
    if not name:
        return entity_id

    record = directory.get_by_name(name)
    if record is None:
        return entity_id
    return record.canonical_id
