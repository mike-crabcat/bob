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
        allow_inbound_dm: int = 1,
    ) -> str:
        contact_id = str(uuid4())
        now_iso = _utcnow_iso()
        await self.db.execute(
            """INSERT INTO contacts (id, name, phone_number, email, is_trusted,
                                     allow_inbound_dm, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (contact_id, name, phone_number, email, is_trusted, allow_inbound_dm,
             now_iso, now_iso),
        )
        return contact_id

    async def update_name(self, contact_id: str, name: str) -> None:
        await self.db.execute(
            "UPDATE contacts SET name = ?, updated_at = ? WHERE id = ?",
            (name, _utcnow_iso(), contact_id))

    async def get_default(self) -> dict | None:
        return await self.db.fetch_one(
            "SELECT * FROM contacts WHERE is_default = 1 AND deleted_at IS NULL LIMIT 1")

    async def list_active(self) -> list[dict]:
        return await self.db.fetch_all(
            "SELECT * FROM contacts WHERE deleted_at IS NULL ORDER BY name")

    async def search_by_name(self, name_like: str) -> dict | None:
        """First live contact whose name matches the LIKE pattern."""
        return await self.db.fetch_one(
            "SELECT * FROM contacts WHERE name LIKE ? AND deleted_at IS NULL LIMIT 1",
            (name_like,))

    async def search(self, pattern: str, limit: int = 20) -> list[dict]:
        """Live contacts whose name, phone, or email matches the LIKE pattern."""
        return await self.db.fetch_all(
            """SELECT * FROM contacts
               WHERE deleted_at IS NULL
                 AND (name LIKE ? OR phone_number LIKE ? OR email LIKE ?)
               ORDER BY name LIMIT ?""",
            (pattern, pattern, pattern, limit))

    async def get_by_name_exact(self, name: str) -> dict | None:
        return await self.db.fetch_one(
            "SELECT * FROM contacts WHERE name = ? AND deleted_at IS NULL LIMIT 1",
            (name,))

    async def get_by_id_prefix(self, prefix: str) -> dict | None:
        return await self.db.fetch_one(
            "SELECT * FROM contacts WHERE id LIKE ? AND deleted_at IS NULL LIMIT 1",
            (f"{prefix}%",))

    async def deleted_ids(self) -> list[str]:
        """Ids of soft-deleted contacts (deletion-propagation sweep)."""
        rows = await self.db.fetch_all(
            "SELECT id FROM contacts WHERE deleted_at IS NOT NULL")
        return [r["id"] for r in rows]

    async def get_any(self, contact_id: str) -> dict | None:
        """Fetch by id including soft-deleted rows (post-write reads)."""
        return await self.db.fetch_one(
            "SELECT * FROM contacts WHERE id = ?", (contact_id,))

    async def create_full(
        self,
        *,
        contact_id: str,
        name: str,
        phone_number: str | None,
        email: str | None,
        metadata_json: str | None,
        allow_inbound_dm: int,
    ) -> None:
        """API-shaped insert with caller-supplied id and metadata.
        Raises the underlying UNIQUE constraint error on conflicts."""
        now_iso = _utcnow_iso()
        await self.db.execute(
            """INSERT INTO contacts (id, name, phone_number, email, metadata,
                                     allow_inbound_dm, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (contact_id, name, phone_number, email, metadata_json,
             allow_inbound_dm, now_iso, now_iso))

    async def list_paged(
        self, *, skip: int = 0, limit: int = 100, search: str | None = None,
    ) -> list[dict]:
        query = "SELECT * FROM contacts WHERE deleted_at IS NULL"
        params: list = []
        if search:
            query += " AND (name LIKE ? OR phone_number LIKE ? OR email LIKE ?)"
            pattern = f"%{search}%"
            params.extend([pattern, pattern, pattern])
        query += " ORDER BY name LIMIT ? OFFSET ?"
        params.extend([limit, skip])
        return await self.db.fetch_all(query, tuple(params))

    _UPDATABLE = {"name", "phone_number", "email", "is_trusted",
                  "allow_inbound_dm", "metadata"}

    async def update_fields(self, contact_id: str, updates: dict) -> None:
        """Partial update of whitelisted columns; bumps updated_at.
        Raises the underlying UNIQUE constraint error on conflicts."""
        bad = set(updates) - self._UPDATABLE
        if bad:
            raise ValueError(f"non-updatable contact columns: {sorted(bad)}")
        cols = {**updates, "updated_at": _utcnow_iso()}
        set_clause = ", ".join(f"{k} = ?" for k in cols)
        await self.db.execute(
            f"UPDATE contacts SET {set_clause} WHERE id = ?",
            (*cols.values(), contact_id))

    async def soft_delete(self, contact_id: str) -> None:
        await self.db.execute(
            "UPDATE contacts SET deleted_at = ? WHERE id = ?",
            (_utcnow_iso(), contact_id))

    async def members_of_group_jid(self, whatsapp_jid: str) -> list[dict]:
        """Live contacts who are current members of a WhatsApp group."""
        return await self.db.fetch_all(
            """SELECT c.* FROM contacts c
               JOIN whatsappgroup_members gm ON gm.contact_id = c.id
               JOIN whatsappgroups g ON g.id = gm.group_id
               WHERE g.whatsapp_jid = ? AND c.deleted_at IS NULL AND gm.left_at IS NULL""",
            (whatsapp_jid,))

    async def set_default(self, contact_id: str) -> None:
        """Mark as the default contact (a trigger unsets others)."""
        await self.db.execute(
            "UPDATE contacts SET is_default = 1 WHERE id = ?", (contact_id,))

    async def clear_default(self) -> None:
        await self.db.execute(
            "UPDATE contacts SET is_default = 0 WHERE is_default = 1")

    async def dashboard_list(self) -> list[dict]:
        """Live contacts with participant rollups (dashboard list view)."""
        return await self.db.fetch_all(
            """SELECT c.id, c.name, c.phone_number, c.email,
                      c.is_trusted, c.is_default, c.allow_inbound_dm,
                      c.created_at, c.updated_at,
                      (SELECT COUNT(*) FROM participants sp WHERE sp.contact_id = c.id) as session_count,
                      (SELECT MAX(sp.last_active_at) FROM participants sp WHERE sp.contact_id = c.id) as last_active
               FROM contacts c
               WHERE c.deleted_at IS NULL
               ORDER BY c.name""")
