"""Loads contacts from the cyborg contacts DB and provides name/UUID lookups."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ContactRecord:
    uuid: str            # full UUID, e.g. "03f3902d-330b-4f15-bf2a-b1385a917677"
    canonical_id: str    # "contact-03f3902d"
    name: str
    phone_number: str
    email: str


class ContactDirectory:
    """In-memory lookup of all contacts in the cyborg contacts DB."""

    def __init__(self, records: list[ContactRecord]) -> None:
        self._by_canonical: dict[str, ContactRecord] = {r.canonical_id: r for r in records}
        self._by_uuid: dict[str, ContactRecord] = {r.uuid: r for r in records}
        # Map of lowercased name -> list of records (handles ambiguity)
        self._by_name_lc: dict[str, list[ContactRecord]] = {}
        for r in records:
            self._by_name_lc.setdefault(r.name.lower(), []).append(r)
            first = r.name.split()[0].lower() if r.name else ""
            if first:
                self._by_name_lc.setdefault(first, []).append(r)

    @classmethod
    async def load(cls, db: Any) -> "ContactDirectory":
        rows = await db.fetch_all(
            "SELECT id, name, phone_number, email FROM contacts "
            "WHERE name IS NOT NULL AND name != '' AND deleted_at IS NULL"
        )
        records = []
        for r in rows:
            uuid = str(r["id"])
            records.append(ContactRecord(
                uuid=uuid,
                canonical_id=f"contact-{uuid[:8]}",
                name=r["name"],
                phone_number=r["phone_number"] or "",
                email=r["email"] or "",
            ))
        return cls(records)

    def get_by_canonical_id(self, canonical_id: str) -> ContactRecord | None:
        return self._by_canonical.get(canonical_id)

    def get_by_uuid(self, uuid: str) -> ContactRecord | None:
        return self._by_uuid.get(uuid)

    def get_by_name(self, name: str) -> ContactRecord | None:
        """Case-insensitive name lookup.

        Tries full-name match first, then first-name. Returns None if no match
        or if multiple distinct contacts share the name (ambiguous).
        """
        key = name.strip().lower()
        if not key:
            return None
        full_matches = self._by_name_lc.get(key, [])
        # Filter out first-name entries that aren't actually full-name matches
        full_only = [r for r in full_matches if r.name.lower() == key]
        if len(full_only) == 1:
            return full_only[0]
        if len(full_only) > 1:
            return None  # ambiguous
        # Fall back to first-name match (only if unique)
        first_only = [r for r in full_matches if r.name.split()[0].lower() == key]
        if len(first_only) == 1:
            return first_only[0]
        return None

    def all_canonical_ids(self) -> set[str]:
        return set(self._by_canonical.keys())

    def as_known_entities(self) -> dict[str, list[dict[str, str]]]:
        """Render as the `known_entities.contacts` hint for the bulletin generator."""
        return {
            "contacts": [
                {"id": r.canonical_id, "display_name": r.name}
                for r in self._by_canonical.values()
            ]
        }
