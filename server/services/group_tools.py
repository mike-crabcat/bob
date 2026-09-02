"""Group tools — participants tool for WhatsApp group sessions."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from server.services.tools import Tool

if TYPE_CHECKING:
    from server.context import AppContext

logger = logging.getLogger(__name__)


def make_group_tools(ctx: AppContext, *, session_key: str) -> list[Tool]:
    """Build tools available to WhatsApp group sessions."""
    db = ctx.db

    async def _participants() -> str:
        """List all current participants in this group with their names, admin status, and contact info."""
        # Resolve the group from session_key via bindings.address -> whatsappgroups.whatsapp_jid
        from server.repositories.conversations import ConversationRepository
        route = await ConversationRepository(db).route_for(session_key)
        if not route or not route["address"]:
            return "Not in a group session."

        from server.repositories.groups import GroupRepository
        groups = GroupRepository(db)
        group = await groups.get_by_jid(route["address"])
        if not group:
            return "Group not found."

        rows = await groups.members_with_contacts(group["id"])

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
