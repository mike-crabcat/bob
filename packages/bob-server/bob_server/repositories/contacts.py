"""ContactRepository — the single SQL path for the contacts table (Bob3 Phase II).

All reads exclude soft-deleted rows. Behaviour is a faithful extraction of the
inline SQL previously scattered across the WhatsApp bridge and email poller;
channel policy (who gets seeded, who gets dropped) does NOT live here — see
services/channel_policies.py.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ContactRepository:
    def __init__(self, db: Any):
        self.db = db

    async def get(self, contact_id: str) -> dict | None:
        return await self.db.fetch_one(
            "SELECT * FROM contacts WHERE id = ? AND deleted_at IS NULL",
            (contact_id,))

    async def is_trusted(self, contact_id: str) -> bool | None:
        """True/False for a live contact, None if no such contact."""
        row = await self.db.fetch_one(
            "SELECT is_trusted FROM contacts WHERE id = ? AND deleted_at IS NULL",
            (contact_id,))
        return None if row is None else bool(row.get("is_trusted", 0))

    async def get_by_phone(self, phone_number: str) -> dict | None:
        return await self.db.fetch_one(
            "SELECT * FROM contacts WHERE phone_number = ? AND deleted_at IS NULL LIMIT 1",
            (phone_number,))

    async def get_by_phone_fuzzy(self, phone_number: str) -> dict | None:
        """Exact match first, then prefix-match fallback.

        The fallback catches WhatsApp JIDs with extra trailing digits
        (e.g. +614154068544 should match existing +61415406854).
        """
        contact = await self.get_by_phone(phone_number)
        if contact:
            return contact
        if len(phone_number) > 6:
            matches = await self.db.fetch_all(
                "SELECT * FROM contacts WHERE deleted_at IS NULL "
                "AND (phone_number = ? OR ? LIKE phone_number || '%' OR phone_number LIKE ? || '%') "
                "ORDER BY LENGTH(phone_number) DESC LIMIT 1",
                (phone_number[:-1], phone_number, phone_number),
            )
            if matches:
                best = matches[0]
                logger.info("resolved contact %s via prefix match: %s → %s",
                            best["id"], phone_number, best["phone_number"])
                return best
        return None

    async def get_by_email(self, email: str) -> dict | None:
        return await self.db.fetch_one(
            "SELECT * FROM contacts WHERE email = ? AND deleted_at IS NULL LIMIT 1",
            (email,))

    async def create(
        self,
        *,
        name: str,
        phone_number: str | None = None,
        email: str | None = None,
        is_trusted: int = 0,
    ) -> str:
        contact_id = str(uuid4())
        now_iso = _utcnow_iso()
        await self.db.execute(
            """INSERT INTO contacts (id, name, phone_number, email, is_trusted,
                                     created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (contact_id, name, phone_number, email, is_trusted, now_iso, now_iso),
        )
        return contact_id

    async def update_name(self, contact_id: str, name: str) -> None:
        await self.db.execute(
            "UPDATE contacts SET name = ?, updated_at = ? WHERE id = ?",
            (name, _utcnow_iso(), contact_id))
