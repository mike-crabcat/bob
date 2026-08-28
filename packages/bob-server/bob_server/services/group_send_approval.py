"""Approval-gated cross-group WhatsApp messages.

The deliberate, narrowly-scoped exception to approval_tools' "approving
records the decision only": a WhatsApp send is a platform-native effect, not
a vendor action, so on approval the platform delivers the stored proposal
verbatim rather than leaving the text to the LLM's discretion on the wake
that follows. Approving one message never changes any group's
``group_outbound_enabled`` policy — there is no standing consent; every
message is approved individually by the owner.

The owner is ``contacts.is_default`` — the exactly-one primary-user flag
the DB maintains by trigger. That column historically meant "notification
routing fallback"; here it also means "human approver", which is the same
person by construction.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from bob_server.context import AppContext

logger = logging.getLogger(__name__)

APPROVAL_TYPE = "group_send"


def send_idempotency_key(approval_id: str) -> str:
    """Deterministic key: a redelivered approval_respond (or a second approve
    attempt) can never produce a second send."""
    return f"whatsapp_group_send_approved:{approval_id}"


async def owner_contact(ctx: AppContext) -> dict[str, Any] | None:
    """The operator, via contacts.is_default (exactly-one, DB-enforced).
    Latent until rollout sets it; request_group_message fails closed then."""
    from bob_server.repositories.contacts import ContactRepository

    return await ContactRepository(ctx.db).get_default()


async def owner_dm_session_key(
    ctx: AppContext, owner: dict[str, Any] | None = None,
) -> str | None:
    """``agent:main:whatsapp:dm:{digits}`` for the owner, but only when an
    active dm binding exists — otherwise wake_session would reject the wake
    (no active route) and the approval would sit unseen."""
    from bob_server.repositories.conversations import ConversationRepository

    if owner is None:
        owner = await owner_contact(ctx)
    if not owner or not owner.get("phone_number"):
        return None
    digits = re.sub(r"\D", "", owner["phone_number"])
    session_key = f"agent:main:whatsapp:dm:{digits}"
    binding = await ConversationRepository(ctx.db).active_binding(session_key)
    if not binding:
        return None
    return session_key


async def create_request(
    ctx: AppContext,
    *,
    group_id: str,
    group_key: str,
    group_name: str | None,
    message: str,
    note: str,
    origin_session_key: str,
    requester_contact_id: str | None,
) -> dict[str, Any]:
    """Owner bypass or a per-message approval routed to the owner's DM.

    Membership and (advisory) rate-limit checks already happened in the tool;
    this owns requester identity, dedupe, and the approval emit. Returns the
    tool's result dict — {"ok", "sent", "approval_id"/"chat_id"} shapes.
    """
    import time

    if not message.strip():
        return {"ok": False, "error": "Message text is empty"}

    owner = await owner_contact(ctx)
    if owner is None:
        return {"ok": False, "error": (
            "No default contact configured — ask the operator to set one "
            "(PUT /api/v1/contacts/{id}/set-default)")}

    owner_dm = await owner_dm_session_key(ctx, owner)
    if owner_dm is None:
        return {"ok": False, "error": (
            f"Owner DM session is not bound ({owner.get('name')}); "
            "cannot route the approval")}

    requester_label = origin_session_key
    if requester_contact_id:
        from bob_server.repositories.contacts import ContactRepository
        requester = await ContactRepository(ctx.db).get(requester_contact_id)
        if requester:
            requester_label = requester["name"]

    # Owner bypass: self-approval is pure friction. Matches on threaded-in
    # contact id, or on the session being the owner's own DM (wake-path
    # group turns carry no contact id).
    if requester_contact_id == str(owner["id"]) or origin_session_key == owner_dm:
        from bob_server.services.whatsapp_outreach_tools import _deliver_group_send

        result = await _deliver_group_send(
            ctx, group_key=group_key, group_id=group_id, message=message,
            origin_session_key=origin_session_key,
            idempotency_key=f"whatsapp_group_send:{group_key}:{uuid4().hex[:8]}",
            provenance={"requested_by": requester_label,
                        "requester_contact_id": requester_contact_id,
                        "owner_direct": True},
            now=time.monotonic())
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error", "delivery failed")}
        return {"ok": True, "sent": True, "chat_id": f"{group_id}@g.us"}

    # Dedupe: the approval_request effect key is a fresh uuid per call, so a
    # repeated LLM attempt would otherwise mint a second pending approval
    # (and a second send once both were approved).
    from bob_server.repositories.approvals import ApprovalRepository

    for row in await ApprovalRepository(ctx.db).pending_of_type(
            APPROVAL_TYPE, entity_id=group_key):
        try:
            proposal = json.loads(row["proposal_data"] or "{}")
        except (TypeError, ValueError):
            continue
        if proposal.get("message") == message:
            return {"ok": True, "sent": False,
                    "approval_id": row["id"], "duplicate": True}

    # The summary is what _summarize_proposal renders into the owner's wake,
    # so it must carry the target group and the exact text being approved.
    proposal = {
        "summary": "\n".join([
            f"Target group: {group_name or group_id} ({group_id})",
            f"Requested by: {requester_label}",
            "",
            "Message to send (verbatim):",
            message,
        ]),
        "group_id": group_id,
        "group_key": group_key,
        "group_name": group_name,
        "message": message,  # the send's source of truth — delivered verbatim
        "origin_session_key": origin_session_key,
        "requester_contact_id": requester_contact_id,
        "requester_label": requester_label,
    }

    from bob_server.services.effects import emit_and_deliver

    result = await emit_and_deliver(
        ctx, kind="approval_request",
        idempotency_key=f"approval_request:{uuid4()}",
        payload={
            "approval_type": APPROVAL_TYPE,
            "entity_id": group_key,
            "title": f"Send message to {group_name or group_id}",
            "description": note,
            "proposal": proposal,
            "requested_by": origin_session_key,
            "origin_conversation_id": owner_dm,
        })
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "approval request failed")}
    return {"ok": True, "sent": False,
            "approval_id": result.get("external_result_id")}


async def on_approved(ctx: AppContext, row: dict[str, Any]) -> None:
    """approval_tools on-approved hook: deliver the stored proposal verbatim.

    Never raises: the decision is already durably recorded, the send carries
    its own idempotency key, and delivery failures are tracked in the effects
    outbox — an exception here would only dead-letter the approval_respond.
    """
    import time

    try:
        proposal = json.loads(row.get("proposal_data") or "{}")
    except (TypeError, ValueError):
        logger.error("group_send approval %s has unparseable proposal; skipped",
                     row.get("id"))
        return
    group_id = proposal.get("group_id")
    message = proposal.get("message")
    if not group_id or message is None:
        logger.error("group_send approval %s proposal lacks group_id/message; skipped",
                     row.get("id"))
        return

    group_key = f"agent:main:whatsapp:group:{group_id}"

    # Execution-time membership re-check: membership is a terminal state —
    # retrying cannot help, and silently sending into a group Bob left would
    # violate the gate the owner just approved.
    from bob_server.repositories.conversations import ConversationRepository

    binding = await ConversationRepository(ctx.db).active_binding(group_key)
    if not binding or binding.get("endpoint_kind") != "group":
        logger.error("group_send approval %s skipped: no active binding for %s",
                     row.get("id"), group_key)
        return

    from bob_server.services.whatsapp_outreach_tools import (
        _deliver_group_send, _group_send_allowed)

    now = time.monotonic()
    if not _group_send_allowed(group_key, now):
        logger.error("group_send approval %s skipped: rate limit for %s "
                     "(owner can re-request later)", row.get("id"), group_key)
        return

    result = await _deliver_group_send(
        ctx, group_key=group_key, group_id=group_id, message=message,
        origin_session_key=proposal.get("origin_session_key") or row.get("requested_by") or "",
        idempotency_key=send_idempotency_key(str(row.get("id"))),
        provenance={"approval_id": row.get("id"),
                    "requested_by": row.get("requested_by"),
                    "requester_contact_id": proposal.get("requester_contact_id"),
                    "group_send_approved": True},
        now=now)
    if not result.get("ok"):
        logger.error("group_send approval %s delivery failed: %s",
                     row.get("id"), result.get("error"))


def register() -> None:
    """Bind on_approved into approval_tools' hook table (idempotent)."""
    from bob_server.services import approval_tools

    approval_tools.register_on_approved(APPROVAL_TYPE, on_approved)
