"""Map non-canonical contact IDs to canonical contact-{hex8} IDs via the contacts DB."""

from __future__ import annotations

import re

from cyborg_server.services.memory.contact_directory import ContactDirectory

_CANONICAL_RE = re.compile(r"^contact-[a-f0-9]{8}$")


def is_canonical_contact_id(entity_id: str) -> bool:
    return bool(_CANONICAL_RE.match(entity_id))


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
    if directory is None or not display_name:
        return entity_id

    record = directory.get_by_name(display_name)
    if record is None:
        return entity_id
    return record.canonical_id
