"""Member relay — ``share_to_group`` (2026-09-17 design).

Non-owners speak THROUGH Bob with their name on it; only the owner can make
Bob act. The relay is pure delivery: the requester — a member of the target
group — provides text and/or a workspace media file, the post goes out under
a mechanically-prepended attribution line, and nothing wakes. No target
turn, no tools, no requester text entering a privileged prompt (the
injection surface of the member-steer it replaces). All composing and
spending still happens in the requesting conversation, where the requester
can see it — the same place in-chat asks already needed no approval.

The prefix is the divider between the two voice regimes: prefixed =
someone else's words relayed (member-gated, this tool), unprefixed = Bob's
own voice (policy-gated ``send_whatsapp_group_message``). Steer
(wake-and-compose) stays the owner's lever; when the relay is enabled,
non-owner human turns carry this tool INSTEAD of steer.

Shares the per-group rate-limit budget with the proactive group send, so
the relay can't be an unlimited lane around it.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING
from uuid import uuid4

from server.services.tools import tool
from server.services.whatsapp_outreach_tools import (
    _deliver_group_send,
    _group_send_allowed,
)

if TYPE_CHECKING:
    from server.context import AppContext


def relay_prefix(requester_name: str) -> str:
    """The attribution line. Built in delivery code, never model-rendered —
    compliance is mechanical, not hoped for."""
    return f"📣 {requester_name} asked me to share:"


def make_relay_tools(
    ctx: AppContext,
    current_session_key: str,
    requester_contact_id: str | None,
    flight: dict | None = None,
) -> list:
    """share_to_group — attached on human-initiated turns when the member
    relay is enabled (config whatsapp_bridge.member_relay_enabled /
    BOB_STEER_MEMBER_RELAY). Non-owner turns carry it INSTEAD of
    steer_conversation; the owner carries both."""

    @tool
    async def share_to_group(group: str, text: str = "",
                             media_path: str = "") -> str:
        """Share EXACT content — text you already have, or a workspace media
        file — into a WhatsApp group the USER belongs to. It posts
        immediately under a "📣 <user> asked me to share:" line — no approval
        queue. THE tool when the user asks to share / post / drop / send
        something they already have into one of their groups ("share this in
        the Leeming Boys chat", "post that mp3 in Bob and Nikesh", "drop
        this clip in my family group"). group: the group name as the user
        said it or a raw group id — must be a group the USER is a member of;
        unknown names return candidate groups, so call the tool and read the
        result. text: the message (or media caption) as it should appear
        under the attribution line — you compose it HERE, in this chat,
        because only you and the requester see this conversation. One of
        text / media_path is required.
        This is DELIVERY, not delegation: nothing wakes in the target. When
        the user wants the target chat to compose its own message or act on
        intent ("let them know", "ask the group something"), that is
        steering — and NEVER do both for the same content: relay OR steer,
        never both (double-post)."""
        from server.services.steering import resolve_target

        text = (text or "").strip()
        media_path = (media_path or "").strip()
        if not text and not media_path:
            return json.dumps(
                {"ok": False, "error": "nothing to share: text or media_path required"})

        resolution = await resolve_target(
            ctx, group, requester_contact_id=requester_contact_id)
        if not resolution.get("ok"):
            return json.dumps(resolution)
        if resolution.get("target_kind") != "group":
            return json.dumps({
                "ok": False,
                "error": "share_to_group targets groups only — a DM needs no relay"})

        group_key = resolution["target_key"]
        group_id = group_key.rsplit(":", 1)[-1]

        requester_name = "someone"
        if requester_contact_id:
            from server.repositories.contacts import ContactRepository
            contact = await ContactRepository(ctx.db).get(requester_contact_id)
            if contact:
                requester_name = contact["name"]

        message = (f"{relay_prefix(requester_name)}\n{text}"
                   if text else relay_prefix(requester_name))

        now = time.monotonic()
        if not _group_send_allowed(group_key, now):
            return json.dumps({"ok": False,
                               "error": "Group send rate limit reached; retry later"})

        from server.services.backburner import active_bg_id
        bg = active_bg_id(flight)
        result = await _deliver_group_send(
            ctx, group_key=group_key, group_id=group_id, message=message,
            origin_session_key=current_session_key,
            idempotency_key=f"member_relay:{group_key}:{uuid4().hex[:8]}",
            provenance={"member_relay": True,
                        "requester_contact_id": requester_contact_id,
                        "requester_name": requester_name,
                        **({"bg_task": bg} if bg else {})},
            now=now, media_path=media_path)
        if not result.get("ok"):
            return json.dumps({"ok": False,
                               "error": result.get("error", "delivery failed")})
        return json.dumps({"ok": True, "chat_id": f"{group_id}@g.us",
                           "shared_with": resolution["target_label"]})

    return [share_to_group]
