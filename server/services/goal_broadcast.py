"""Owner-approved goal broadcasts (2026-10-09, the Night Watch noon post).

Mike asked for "a 12pm broadcast to all groups active this week" and no
autonomous path could deliver it: routines post only to their own chat,
goal rooms carry no group-send tool, and send_whatsapp_group_message is
policy-gated off for most groups. Bob still told the group the broadcast
was "scheduled".

A broadcast grant closes that gap without loosening the gates. It lives on
the goal (payload_json.broadcast_grant): an explicit list of group JIDs,
who granted it, and an expiry. Only the owner can grant — from a human
turn of their own (``authorize_goal_broadcast``) or the operator CLI
(``bob goal-rooms grant-broadcast``). The goal's room then carries
``goal_broadcast``, which posts only to granted groups Bob is still bound
to, once per (group, content) — a retried round can't double-post.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from server.services.tools import tool

if TYPE_CHECKING:
    from server.context import AppContext

logger = logging.getLogger(__name__)

GRANT_KEY = "broadcast_grant"
DEFAULT_TTL = timedelta(hours=48)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _payload(goal: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(goal.get("payload_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def active_grant(goal: dict[str, Any] | None,
                 now: datetime | None = None) -> dict[str, Any] | None:
    """The goal's unexpired broadcast grant, or None."""
    if not goal or goal.get("status") != "active":
        return None
    grant = _payload(goal).get(GRANT_KEY)
    if not isinstance(grant, dict) or not grant.get("groups"):
        return None
    expires = _parse_iso(grant.get("expires_at"))
    if expires is not None and (now or datetime.now(UTC)) >= expires:
        return None
    return grant


def _gid(jid_or_key: str) -> str:
    """Normalise a group reference to its bare id (no @g.us, no key prefix)."""
    s = (jid_or_key or "").strip()
    if s.startswith("agent:main:whatsapp:group:"):
        s = s.split(":", 4)[-1]
    return s.split("@")[0]


async def resolve_groups(ctx: AppContext, refs: list[str]) -> tuple[list[dict[str, str]], list[str]]:
    """Resolve names / JIDs / session keys to groups Bob is ACTIVELY bound
    to. Returns (resolved [{gid, name}], errors)."""
    from server.repositories.groups import GroupRepository

    bound = await GroupRepository(ctx.db).actively_bound_groups()
    by_gid = {_gid(g["whatsapp_jid"]): g["name"] for g in bound}
    out: list[dict[str, str]] = []
    errors: list[str] = []
    for ref in refs:
        ref = ref.strip()
        if not ref:
            continue
        gid = _gid(ref)
        if re.fullmatch(r"[\d-]{10,}", gid):
            if gid in by_gid:
                out.append({"gid": gid, "name": by_gid[gid]})
            else:
                errors.append(f"{ref}: Bob is not an active member of that group")
            continue
        needle = ref.casefold()
        hits = [(g, n) for g, n in by_gid.items() if needle in (n or "").casefold()]
        exact = [(g, n) for g, n in hits if (n or "").casefold() == needle]
        hits = exact or hits
        if not hits:
            from server.services.group_tools import groups_by_alias
            hits = [(_gid(a["whatsapp_jid"]), by_gid[_gid(a["whatsapp_jid"])])
                    for a in await groups_by_alias(ctx.db, ref)
                    if _gid(a["whatsapp_jid"]) in by_gid]
        if len(hits) == 1:
            out.append({"gid": hits[0][0], "name": hits[0][1]})
        elif not hits:
            errors.append(f"{ref}: no group Bob is in matches that name")
        else:
            errors.append(f"{ref}: ambiguous — " + ", ".join(
                f"{n} ({g})" for g, n in hits[:6]))
    seen: set[str] = set()
    uniq = [g for g in out if not (g["gid"] in seen or seen.add(g["gid"]))]
    return uniq, errors


async def grant_broadcast(ctx: AppContext, goal_id: str, refs: list[str], *,
                          granted_by: str, until: str = "",
                          note: str = "") -> dict[str, Any]:
    """Record (replace) the goal's broadcast grant. Caller owns the
    authorisation check (owner turn / operator CLI)."""
    from server.repositories.goals import GoalRepository

    repo = GoalRepository(ctx.db)
    goal = await repo.get(goal_id)
    if goal is None or goal["status"] != "active":
        return {"ok": False, "error": "no active goal with that id"}
    groups, errors = await resolve_groups(ctx, refs)
    if errors:
        return {"ok": False, "error": "; ".join(errors)}
    if not groups:
        return {"ok": False, "error": "no groups given"}
    now = datetime.now(UTC)
    expires = _parse_iso(until)
    if until and expires is None:
        return {"ok": False, "error": f"until is not an ISO instant: {until!r}"}
    if expires is None:
        deadline = _parse_iso(goal.get("deadline"))
        expires = (deadline + timedelta(hours=2)) if deadline and deadline > now \
            else now + DEFAULT_TTL
    grant = {"groups": groups, "granted_by": granted_by,
             "granted_at": now.isoformat(), "expires_at": expires.isoformat(),
             "note": note[:300]}
    await repo.set_payload_key(goal_id, GRANT_KEY, grant)
    logger.info("goal %s: broadcast grant to %d group(s) by %s until %s",
                goal_id, len(groups), granted_by, grant["expires_at"])
    return {"ok": True, "goal_id": goal_id, **grant}


async def revoke_broadcast(ctx: AppContext, goal_id: str) -> bool:
    from server.repositories.goals import GoalRepository
    return await GoalRepository(ctx.db).set_payload_key(goal_id, GRANT_KEY, None)


def render_grant(grant: dict[str, Any] | None) -> str:
    """One line for room prompts / state views."""
    if not grant:
        return ""
    from server.services.base import local_iso
    names = ", ".join(f"{g['name']} ({g['gid']})" for g in grant["groups"])
    return (f"Broadcast grant (owner-approved, until {local_iso(grant['expires_at'])}): "
            f"post with goal_broadcast to — {names}")


def make_grant_tools(ctx: AppContext, session_key: str, contact_id: str) -> list:
    """Owner-only, human-initiated turns: turn an explicit broadcast request
    into a grant the goal's room can act on later."""

    @tool
    async def authorize_goal_broadcast(goal_id: str, groups: str,
                                       until: str = "", note: str = "") -> str:
        """Record the OWNER's explicit request that a goal post to specific
        WhatsApp groups later (e.g. "broadcast at noon to all groups active
        this week"). Without this, nothing autonomous can post to other
        groups: routines only reach their own chat and goal rooms have no
        group-send. ``groups`` = comma-separated group names or JIDs — list
        the exact groups (resolve "active this week" yourself first, and
        name them back to the owner in your reply). ``until`` = ISO instant
        the grant lapses (default: goal deadline + 2h, else 48h). The goal's
        room then gets goal_broadcast for exactly these groups. Owner only;
        call it in the same turn as the request."""
        from server.services.steering import owner_contact
        owner = await owner_contact(ctx)
        if not owner or str(owner["id"]) != str(contact_id):
            return json.dumps({"ok": False, "error":
                               "only the owner can authorise a goal broadcast"})
        refs = [r for r in re.split(r"[,\n]", groups or "") if r.strip()]
        result = await grant_broadcast(ctx, goal_id, refs,
                                       granted_by=str(contact_id), until=until,
                                       note=note or f"requested in {session_key}")
        return json.dumps(result, ensure_ascii=False)

    return [authorize_goal_broadcast]


def _content_key(goal_id: str, gid: str, message: str, media_path: str) -> str:
    digest = hashlib.sha256(f"{message}\x00{media_path}".encode()).hexdigest()[:16]
    return f"goal_broadcast:{goal_id}:{gid}:{digest}"


def make_broadcast_tools(ctx: AppContext, room_session_key: str) -> list:
    """Room tool: post into the goal's granted groups. Attached only while
    the goal holds an active grant (room_turn_tools checks)."""

    @tool
    async def goal_broadcast(group: str, message: str, media_path: str = "") -> str:
        """Post a message (optionally with an image/video: media_path,
        workspace-relative; message becomes the caption) into ONE group
        covered by this goal's owner-approved broadcast grant. One call per
        group; the same content to the same group is delivered once (a
        repeat call is a no-op that returns the first receipt). Groups
        outside the grant, or that Bob has left, are refused."""
        from server.repositories.effects import EffectRepository
        from server.services.goal_rooms import _room_goal
        from server.services.whatsapp_outreach_tools import (
            _deliver_group_send, _group_send_allowed,
        )

        bridge = getattr(ctx, "whatsapp_bridge", None)
        if bridge is None or not getattr(bridge, "connected", False):
            return json.dumps({"ok": False, "error": "WhatsApp bridge is not connected"})
        goal = await _room_goal(ctx, room_session_key)
        grant = active_grant(goal)
        if goal is None or grant is None:
            return json.dumps({"ok": False, "error":
                               "this goal has no active broadcast grant"})
        granted = {g["gid"]: g["name"] for g in grant["groups"]}
        resolved, errors = await resolve_groups(ctx, [group])
        if errors or not resolved:
            return json.dumps({"ok": False, "error": "; ".join(errors) or "unknown group"})
        gid = resolved[0]["gid"]
        if gid not in granted:
            return json.dumps({"ok": False, "error":
                               f"{resolved[0]['name']} is not in this goal's grant "
                               f"({', '.join(granted.values())})"})
        key = _content_key(goal["id"], gid, message, media_path)
        prior = await EffectRepository(ctx.db).get_by_key(key)
        if prior and prior.get("status") in ("pending", "delivering", "delivered"):
            return json.dumps({"ok": True, "already_sent": True,
                               "group": granted[gid]})
        group_key = f"agent:main:whatsapp:group:{gid}"
        now = time.monotonic()
        if not _group_send_allowed(group_key, now):
            return json.dumps({"ok": False, "error": "group send rate limit reached; retry later"})
        result = await _deliver_group_send(
            ctx, group_key=group_key, group_id=gid, message=message,
            origin_session_key=room_session_key, idempotency_key=key,
            provenance={"goal_id": goal["id"],
                        "broadcast_grant": grant.get("granted_by")},
            now=now, media_path=media_path)
        if not result.get("ok"):
            return json.dumps({"ok": False, "error": result.get("error", "delivery failed")})
        return json.dumps({"ok": True, "group": granted[gid],
                           "receipt": result.get("external_result_id")})

    return [goal_broadcast]
