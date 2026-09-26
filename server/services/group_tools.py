"""Group tools — participants tool for WhatsApp group sessions."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from server.services.tools import Tool

if TYPE_CHECKING:
    from server.context import AppContext

logger = logging.getLogger(__name__)


async def _render_roster(groups, group) -> str:
    """Shared roster rendering for both participant tools."""
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

        return await _render_roster(groups, group)

    return [
        Tool(
            name="participants",
            description="List all current participants in this WhatsApp group with their names, admin status, and whether they are a known contact. Use this when you need to know who is in the group.",
            parameters={},
            required=[],
            handler=_participants,
        ),
    ]


def make_group_lookup_tools(
    ctx: AppContext, *, is_trusted: bool = False,
    contact_id: str | None = None, session_key: str | None = None,
) -> list[Tool]:
    """Cross-session roster lookup — the counterpart to the session-scoped
    ``participants`` tool for conversations that are NOT the group itself
    (owner DMs planning outreach, routine turns). Scoping mirrors
    session_tools: trusted conversations may query any group; others only
    groups their contact participates in, plus the session's own group."""
    db = ctx.db

    async def _group_participants(group: str) -> str:
        """List the live WhatsApp participants of a group by name or JID."""
        from server.repositories.groups import GroupRepository
        groups = GroupRepository(db)

        q = (group or "").strip()
        if not q:
            return "Error: group cannot be empty — pass a group name or JID."

        # JID-shaped queries resolve directly; names go through LIKE search
        # (search_by_name takes the pattern raw, wildcards included).
        candidates: list[dict] = []
        if "@g.us" in q:
            hit = await groups.get_by_jid(q)
            candidates = [hit] if hit else []
        elif q.isdigit():
            hit = await groups.get_by_jid(f"{q}@g.us")
            candidates = [hit] if hit else []
        if not candidates:
            name_hits = await groups.search_by_name(f"%{q}%")
            exact = [g for g in name_hits if g["name"].lower() == q.lower()]
            # search_by_name returns (jid, name) only — re-fetch full rows
            for g in (exact or name_hits)[:6]:
                full = await groups.get_by_jid(g["whatsapp_jid"])
                if full:
                    candidates.append(full)
        if not candidates:
            return f"No group matching {q!r}."
        if len(candidates) > 1:
            names = ", ".join(f"{g['name']} ({g['whatsapp_jid']})" for g in candidates[:6])
            return f"Multiple groups match {q!r} — pass the full name or JID: {names}"

        target = candidates[0]

        if not is_trusted:
            allowed: set[str] = set()
            if contact_id:
                allowed |= {
                    g["whatsapp_jid"]
                    for g in await groups.groups_for_contact(contact_id)
                }
            if session_key:
                from server.repositories.conversations import ConversationRepository
                route = await ConversationRepository(db).route_for(session_key)
                if route and route.get("address"):
                    allowed.add(route["address"])
            if target["whatsapp_jid"] not in allowed:
                return ("Not allowed: this conversation can only list "
                        "participants of groups its contact belongs to.")

        return await _render_roster(groups, target)

    return [
        Tool(
            name="group_participants",
            description=(
                "List the live WhatsApp participants of a group by name or JID — "
                "names, admin status, phone numbers, trusted badges. Unlike "
                "`participants` (which only works inside a group session), this "
                "works from any conversation — use it for outreach planning and "
                "for checking a remembered roster against the live one. Trusted "
                "conversations may query any group; others only groups they "
                "belong to."
            ),
            parameters={
                "group": {
                    "type": "string",
                    "description": "Group name (substring match) or WhatsApp JID",
                },
            },
            required=["group"],
            handler=_group_participants,
        ),
    ]
