"""WhatsApp group repository — owns whatsappgroups & whatsappgroup_members.

Group rows mirror bridge state (name, description, member_count) and carry
the optional memory_entity_id link into the memory system. Membership is
soft-departed via left_at.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4


class GroupRepository:
    def __init__(self, db: Any):
        self.db = db

    # ------------------------------------------------------------- groups

    async def get_by_jid(self, whatsapp_jid: str) -> dict[str, Any] | None:
        row = await self.db.fetch_one(
            "SELECT * FROM whatsappgroups WHERE whatsapp_jid = ? AND deleted_at IS NULL",
            (whatsapp_jid,))
        return dict(row) if row else None

    async def upsert_group(
        self,
        whatsapp_jid: str,
        *,
        name: str,
        description: str,
        member_count: int,
        now_iso: str,
    ) -> str:
        """Full-sync upsert: refresh metadata on an existing group or create
        one. Returns the group id."""
        existing = await self.get_by_jid(whatsapp_jid)
        if existing:
            await self.db.execute(
                "UPDATE whatsappgroups SET name = ?, description = ?, member_count = ?, updated_at = ? WHERE id = ?",
                (name, description, member_count, now_iso, existing["id"]))
            return existing["id"]
        group_id = str(uuid4())
        await self.db.execute(
            """INSERT INTO whatsappgroups (id, whatsapp_jid, name, description, member_count, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (group_id, whatsapp_jid, name, description, member_count, now_iso, now_iso))
        return group_id

    async def ensure_group(self, whatsapp_jid: str, name: str, now_iso: str) -> str:
        """Resolve a group id by jid, creating a stub row if unseen."""
        existing = await self.get_by_jid(whatsapp_jid)
        if existing:
            return existing["id"]
        group_id = str(uuid4())
        await self.db.execute(
            """INSERT INTO whatsappgroups (id, whatsapp_jid, name, member_count, created_at, updated_at)
               VALUES (?, ?, ?, 0, ?, ?)""",
            (group_id, whatsapp_jid, name, now_iso, now_iso))
        return group_id

    async def refresh_member_count(self, group_id: str, now_iso: str) -> int:
        row = await self.db.fetch_one(
            "SELECT COUNT(*) as cnt FROM whatsappgroup_members WHERE group_id = ? AND left_at IS NULL",
            (group_id,))
        count = row["cnt"] if row else 0
        await self.db.execute(
            "UPDATE whatsappgroups SET member_count = ?, updated_at = ? WHERE id = ?",
            (count, now_iso, group_id))
        return count

    async def set_memory_entity(self, group_id: str, entity_id: str) -> None:
        await self.db.execute(
            "UPDATE whatsappgroups SET memory_entity_id = ? WHERE id = ?",
            (entity_id, group_id))

    async def memory_entity_id(self, whatsapp_jid: str) -> str | None:
        row = await self.db.fetch_one(
            "SELECT memory_entity_id FROM whatsappgroups WHERE whatsapp_jid = ? AND deleted_at IS NULL",
            (whatsapp_jid,))
        return row["memory_entity_id"] if row and row["memory_entity_id"] else None

    # ------------------------------------------------------------ members

    async def upsert_member(
        self,
        group_id: str,
        contact_id: str,
        *,
        display_name: str,
        now_iso: str,
        is_admin: int | None = None,
        is_super_admin: int | None = None,
    ) -> None:
        """Upsert a membership (re-joining clears left_at). Admin flags are
        only written when provided (full sync); join events omit them."""
        if is_admin is None:
            await self.db.execute(
                """INSERT INTO whatsappgroup_members (id, group_id, contact_id, display_name, joined_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(group_id, contact_id) DO UPDATE SET
                       left_at = NULL,
                       joined_at = excluded.joined_at,
                       display_name = COALESCE(excluded.display_name, whatsappgroup_members.display_name),
                       updated_at = excluded.updated_at""",
                (str(uuid4()), group_id, contact_id, display_name, now_iso, now_iso, now_iso))
        else:
            await self.db.execute(
                """INSERT INTO whatsappgroup_members (id, group_id, contact_id, is_admin, is_super_admin, display_name, joined_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(group_id, contact_id) DO UPDATE SET
                       is_admin = excluded.is_admin,
                       is_super_admin = excluded.is_super_admin,
                       display_name = excluded.display_name,
                       left_at = NULL,
                       updated_at = excluded.updated_at""",
                (str(uuid4()), group_id, contact_id, is_admin, is_super_admin or 0,
                 display_name, now_iso, now_iso, now_iso))

    async def mark_departed_except(
        self, group_id: str, keep_contact_ids: set[str], now_iso: str,
    ) -> None:
        """Soft-depart every current member not in the keep set (full sync)."""
        if not keep_contact_ids:
            return
        placeholders = ",".join("?" for _ in keep_contact_ids)
        await self.db.execute(
            f"UPDATE whatsappgroup_members SET left_at = ?, updated_at = ? "
            f"WHERE group_id = ? AND left_at IS NULL AND contact_id NOT IN ({placeholders})",
            (now_iso, now_iso, group_id, *keep_contact_ids))

    async def mark_left(self, group_id: str, contact_id: str, now_iso: str) -> None:
        await self.db.execute(
            "UPDATE whatsappgroup_members SET left_at = ?, updated_at = ? "
            "WHERE group_id = ? AND contact_id = ? AND left_at IS NULL",
            (now_iso, now_iso, group_id, contact_id))

    # -------------------------------------------------------------- reads

    async def members_with_contacts(self, group_id: str) -> list[dict[str, Any]]:
        """Current members with their contact rows joined, admins first."""
        rows = await self.db.fetch_all(
            """SELECT gm.display_name, gm.is_admin, gm.is_super_admin,
                      c.name as contact_name, c.phone_number, c.is_trusted
               FROM whatsappgroup_members gm
               JOIN contacts c ON c.id = gm.contact_id AND c.deleted_at IS NULL
               WHERE gm.group_id = ? AND gm.left_at IS NULL
               ORDER BY gm.is_super_admin DESC, gm.is_admin DESC, gm.display_name ASC""",
            (group_id,))
        return [dict(r) for r in rows] if rows else []

    async def member_contact_ids(self, whatsapp_jid: str) -> list[str]:
        """Contact ids of a group's current members (memory extraction)."""
        rows = await self.db.fetch_all(
            "SELECT gm.contact_id FROM whatsappgroup_members gm "
            "JOIN contacts c ON c.id = gm.contact_id "
            "WHERE gm.group_id = (SELECT id FROM whatsappgroups WHERE whatsapp_jid = ?) "
            "AND gm.left_at IS NULL",
            (whatsapp_jid,))
        return [r["contact_id"] for r in rows]

    async def groups_for_contact(self, contact_id: str) -> list[dict[str, Any]]:
        """Live groups a contact currently belongs to."""
        rows = await self.db.fetch_all(
            """SELECT g.name, g.whatsapp_jid, gm.is_admin, gm.joined_at
               FROM whatsappgroup_members gm
               JOIN whatsappgroups g ON g.id = gm.group_id
               WHERE gm.contact_id = ? AND gm.left_at IS NULL AND g.deleted_at IS NULL
               ORDER BY g.name""",
            (contact_id,))
        return [dict(r) for r in rows] if rows else []
