"""Group tools — participants tool for WhatsApp group sessions."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cyborg_server.services.tools import Tool

if TYPE_CHECKING:
    from cyborg_server.context import AppContext

logger = logging.getLogger(__name__)


def make_group_tools(ctx: AppContext, *, session_key: str) -> list[Tool]:
    """Build tools available to WhatsApp group sessions."""
    db = ctx.db

    async def _participants() -> str:
        """List all current participants in this group with their names, admin status, and contact info."""
        # Resolve the group from session_key via session_routes.chat_id -> whatsappgroups.whatsapp_jid
        route = await db.fetch_one(
            "SELECT chat_id FROM session_routes WHERE session_key = ?",
            (session_key,),
        )
        if not route or not route["chat_id"]:
            return "Not in a group session."

        group = await db.fetch_one(
            "SELECT id, name, member_count FROM whatsappgroups WHERE whatsapp_jid = ? AND deleted_at IS NULL",
            (route["chat_id"],),
        )
        if not group:
            return "Group not found."

        rows = await db.fetch_all(
            """SELECT gm.display_name, gm.is_admin, gm.is_super_admin,
                      c.name as contact_name, c.phone_number, c.is_trusted
               FROM whatsappgroup_members gm
               JOIN contacts c ON c.id = gm.contact_id AND c.deleted_at IS NULL
               WHERE gm.group_id = ? AND gm.left_at IS NULL
               ORDER BY gm.is_super_admin DESC, gm.is_admin DESC, gm.display_name ASC""",
            (group["id"],),
        )

        lines = [f"Group: {group['name']} ({len(rows)} members)"]
        for r in rows:
            name = r["display_name"] or r["contact_name"] or r["phone_number"]
            badges = []
            if r["is_super_admin"]:
                badges.append("super admin")
            elif r["is_admin"]:
                badges.append("admin")
            if r["is_trusted"]:
                badges.append("trusted")
            badge_str = f" ({', '.join(badges)})" if badges else ""
            lines.append(f"- {name}{badge_str} — {r['phone_number']}")

        return "\n".join(lines)

    return [
        Tool(
            name="participants",
            description="List all current participants in this WhatsApp group with their names, admin status, and whether they are a known contact. Use this when you need to know who is in the group.",
            parameters={},
            required=[],
            handler=_participants,
        ),
    ]
