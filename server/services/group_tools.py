"""Group tools — participants tool for WhatsApp group sessions."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

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


async def groups_by_alias(db: Any, name: str) -> list[dict[str, Any]]:
    """WhatsApp groups (name, whatsapp_jid) whose memory entity carries
    ``name`` as an alias — the fallback when the live title doesn't match."""
    from server.repositories.groups import GroupRepository
    from server.services.memory.entity_resolver import group_entities_by_alias
    return await GroupRepository(db).by_memory_entities(
        await group_entities_by_alias(db, name))


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
            for g in (await groups_by_alias(db, q))[:6]:
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

    async def _list_groups(active_since: str = "") -> str:
        """Every WhatsApp group Bob is currently in, with last human activity."""
        import re as _re
        from datetime import UTC, datetime, timedelta

        from server.repositories.conversations import ConversationRepository
        from server.repositories.groups import GroupRepository
        from server.repositories.history import HistoryRepository
        from server.services.base import local_iso
        from server.services.steering import owner_contact

        since_dt = None
        spec = (active_since or "").strip().lower()
        if spec:
            m = _re.fullmatch(r"(\d+)\s*d(ays?)?", spec)
            if m:
                since_dt = datetime.now(UTC) - timedelta(days=int(m.group(1)))
            else:
                try:
                    since_dt = datetime.fromisoformat(spec.replace("z", "+00:00"))
                except ValueError:
                    return ("Error: active_since must be an ISO date/datetime "
                            "(local midnight Monday = start of 'this week') or 'Nd'.")
                if since_dt.tzinfo is None:  # naive = server-local wall clock
                    since_dt = since_dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
        since_iso = (since_dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
                     if since_dt else "")

        groups = GroupRepository(db)
        bound = await groups.actively_bound_groups()
        if not is_trusted:
            allowed = {g["whatsapp_jid"] for g in
                       await groups.groups_for_contact(contact_id)} if contact_id else set()
            bound = [g for g in bound if g["whatsapp_jid"] in allowed]
        conv = ConversationRepository(db)
        keys = {g["whatsapp_jid"]: f"agent:main:whatsapp:group:{g['whatsapp_jid'].split('@')[0]}"
                for g in bound}
        cids = {jid: await conv.resolve_cid(k) for jid, k in keys.items()}
        activity = await HistoryRepository(db).human_activity(
            list(cids.values()), since_iso=since_iso)
        owner = await owner_contact(ctx)
        rows = []
        for g in bound:
            jid = g["whatsapp_jid"]
            act = activity.get(cids[jid], {})
            if since_iso and not act.get("n_since"):
                continue
            members = await groups.member_contact_ids(jid)
            policy = await conv.get_policy(keys[jid])
            rows.append((act.get("last_at") or "", g["name"], jid, keys[jid],
                         act.get("n_since", 0), len(members),
                         bool(owner and str(owner["id"]) in {str(m) for m in members}),
                         bool(policy.get("group_outbound_enabled"))))
        rows.sort(key=lambda r: r[0], reverse=True)
        if not rows:
            return "No groups match." if since_iso else "Bob is in no groups."
        head = (f"{len(rows)} group(s) Bob is in"
                + (f" with human messages since {local_iso(since_dt.isoformat())}" if since_dt else "")
                + " (newest activity first):")
        lines = [head]
        for last_at, name, jid, key, n, members, owner_in, outbound in rows:
            last = local_iso(last_at) if last_at else "never"
            lines.append(
                f"- {name} — {jid} ({key}) · last human msg {last}"
                + (f" · {n} since" if since_iso else "")
                + f" · {members} members · owner {'in' if owner_in else 'NOT in'}"
                + f" · autonomous group-send {'on' if outbound else 'off'}")
        return "\n".join(lines)

    return [
        Tool(
            name="list_groups",
            description=(
                "Every WhatsApp group Bob is currently a member of, in ONE call: "
                "name, JID, session key, last human message time, member count, "
                "whether the owner is in it, and whether autonomous group-send is "
                "on. `active_since` (ISO date/datetime, local time, or 'Nd') keeps "
                "only groups with human messages since then — e.g. 'all groups "
                "active this week' = active_since=<Monday's date>. Use this "
                "instead of probing find_session / get_session_messages per group."
            ),
            parameters={
                "active_since": {
                    "type": "string",
                    "description": "Optional ISO date/datetime (local) or 'Nd' (e.g. '7d')",
                },
            },
            required=[],
            handler=_list_groups,
        ),
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
