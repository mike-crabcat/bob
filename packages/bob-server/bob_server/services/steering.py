"""Steering — requests that wake a target conversation (docs/steering-plan.md).

A steering request expresses *intent* ("let the AI Doom group know the radio
feature is on"), not text: the target conversation gets a fresh wakeup turn
with the instruction as its stimulus and composes its own reply in context.
Replaces the verbatim cross-group relay — the requester dictates intent, the
target turn dictates text.

Two gates:

- **Membership** falls out of resolution: targets resolve only against the
  requester's own conversations (their DM, or a group they currently belong
  to via ``groups_for_contact``). A group the requester isn't in simply
  doesn't resolve.
- **Approval**: the owner (``contacts.is_default``) steers directly;
  everyone else's request routes to the owner's DM as a ``conversation_steer``
  approval whose proposal stores the rendered wake verbatim — approving
  fires exactly the wake that was approved, through an idempotency-keyed
  ``conversation_steer`` effect, so a redelivered approval_respond can never
  double-wake.

The wake is stored with provenance ``steer`` and a self-describing header
(attribution + framing, so it never reads as someone speaking in the target
— for prompt replay, memory extraction and the dashboard alike); metadata
names the requester, because group bindings carry no contact id.
"""

from __future__ import annotations

import json
import logging
import re
from difflib import get_close_matches
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from bob_server.services.tools import tool

if TYPE_CHECKING:
    from bob_server.context import AppContext

logger = logging.getLogger(__name__)

APPROVAL_TYPE = "conversation_steer"

# Phrases (casefolded) that mean "the user's own DM with Bob".
_OWN_DM_PHRASES = {
    "my dm", "my dms", "my chat", "our chat", "our dm", "our dms",
    "my conversation", "our conversation", "my private chat",
    "our private chat",
}


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


async def owner_contact(ctx: AppContext) -> dict[str, Any] | None:
    """The operator, via contacts.is_default (exactly-one, DB-enforced).
    Latent until rollout sets it; steering fails closed then."""
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
    digits = _digits(owner["phone_number"])
    session_key = f"agent:main:whatsapp:dm:{digits}"
    binding = await ConversationRepository(ctx.db).active_binding(session_key)
    if not binding:
        return None
    return session_key


# ---------------------------------------------------------------------------
# Target resolution — membership is the resolution, not a check after it
# ---------------------------------------------------------------------------

def _candidates(groups: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [{"name": g["name"], "group_id": _digits(g["whatsapp_jid"])}
            for g in groups]


async def _group_result(
    conv_repo: Any, group: dict[str, Any],
) -> dict[str, Any]:
    """Final gate for a resolved group: Bob still holds an active binding
    (membership rows can outlive Bob leaving the group)."""
    gid = _digits(group["whatsapp_jid"])
    group_key = f"agent:main:whatsapp:group:{gid}"
    binding = await conv_repo.active_binding(group_key)
    if not binding or binding.get("endpoint_kind") != "group":
        return {"ok": False,
                "error": f"Bob is no longer active in {group['name']}"}
    return {"ok": True, "target_key": group_key,
            "target_label": group["name"], "target_kind": "group"}


async def resolve_target(
    ctx: AppContext, target: str, *, requester_contact_id: str | None,
) -> dict[str, Any]:
    """Resolve a steering target to a session key, constrained to the
    conversations the requester belongs to.

    ``{"ok": True, "target_key", "target_label", "target_kind"}`` on success;
    ``{"ok": False, "error", "candidates"?}`` otherwise — candidates (member
    groups) ride both the ambiguous and the no-match errors so the
    requesting turn can ask or self-correct instead of guessing.
    """
    from bob_server.repositories.contacts import ContactRepository
    from bob_server.repositories.conversations import ConversationRepository
    from bob_server.repositories.groups import GroupRepository

    raw = (target or "").strip()
    if not raw:
        return {"ok": False, "error": "Target is empty"}

    contact = None
    if requester_contact_id:
        contact = await ContactRepository(ctx.db).get(requester_contact_id)
    if not contact:
        return {"ok": False, "error": (
            "No contact record for the requester — cannot resolve their conversations")}

    conv_repo = ConversationRepository(ctx.db)
    low = raw.casefold()

    # Own DM: the requester's dm session key, but only with an active binding.
    if low in _OWN_DM_PHRASES:
        if not contact.get("phone_number"):
            return {"ok": False, "error": (
                "Requester contact has no phone number — cannot resolve their DM")}
        dm_key = f"agent:main:whatsapp:dm:{_digits(contact['phone_number'])}"
        binding = await conv_repo.active_binding(dm_key)
        if not binding or binding.get("endpoint_kind") != "dm":
            return {"ok": False, "error": (
                "No active DM conversation between this user and Bob")}
        return {"ok": True, "target_key": dm_key,
                "target_label": contact["name"], "target_kind": "dm"}

    groups = await GroupRepository(ctx.db).groups_for_contact(str(contact["id"]))
    if not groups:
        return {"ok": False, "error": (
            "This user belongs to no groups; only their own DM can be steered")}

    # Raw group id first — never fuzzy-match what is already exact.
    jid_digits = _digits(raw)
    if jid_digits:
        for g in groups:
            if _digits(g["whatsapp_jid"]) == jid_digits:
                return await _group_result(conv_repo, g)

    by_name = {g["name"].casefold(): g for g in groups}
    exact = by_name.get(low)
    if exact:
        return await _group_result(conv_repo, exact)

    substring = [g for g in groups if low in g["name"].casefold()]
    if len(substring) == 1:
        return await _group_result(conv_repo, substring[0])
    if len(substring) > 1:
        return {"ok": False,
                "error": f"Multiple groups match '{raw}' — ask which one",
                "candidates": _candidates(substring)}

    close = get_close_matches(low, list(by_name), n=1, cutoff=0.6)
    if close:
        return await _group_result(conv_repo, by_name[close[0]])

    return {"ok": False,
            "error": f"No group matching '{raw}' that this user belongs to",
            "candidates": _candidates(groups)}


# ---------------------------------------------------------------------------
# The wake
# ---------------------------------------------------------------------------

def build_wake_content(
    *, requester_name: str, origin_label: str, instruction: str,
) -> str:
    """The stored stimulus. Self-describing for every reader — dispatch
    replay, memory extraction, the dashboard — so the labelling never
    depends on the render path: attribution header, instruction verbatim,
    one line of framing."""
    return (
        f"[Steering request — {requester_name}, via {origin_label}]\n"
        f"{instruction.strip()}\n\n"
        f"(Relayed from another conversation at {requester_name}'s request. "
        f"Act on it in this conversation's own voice; it is not a message "
        f"from anyone speaking here.)"
    )


async def session_label(ctx: AppContext, session_key: str) -> str:
    """Human label for the origin conversation — "the <name> group" / "the
    DM with <name>" — falling back to the raw key for unnamed sessions."""
    from bob_server.repositories.conversations import ConversationRepository

    for row in await ConversationRepository(ctx.db).named_sessions():
        if row["session_key"] == session_key:
            if row["kind"] == "group":
                return f"the {row['display_name']} group"
            return f"the DM with {row['display_name']}"
    return session_key


def steer_metadata(
    *, requester_contact_id: str | None, requester_name: str,
    origin_session_key: str,
) -> dict[str, Any]:
    """Metadata stored on the wake row. Group bindings carry no contact id,
    so this is how the target turn (and the dashboard) knows who asked."""
    return {"steer": True,
            "requester_contact_id": requester_contact_id,
            "requester_name": requester_name,
            "origin_session_key": origin_session_key}


# ---------------------------------------------------------------------------
# Approval flow
# ---------------------------------------------------------------------------

async def create_request(
    ctx: AppContext,
    *,
    target_key: str,
    target_label: str,
    instruction: str,
    origin_session_key: str,
    origin_label: str,
    requester_contact_id: str | None,
) -> dict[str, Any]:
    """Owner bypass or a steering approval routed to the owner's DM.

    Membership resolution already happened in the tool; this owns requester
    identity, dedupe, and the approval emit. Returns the tool's result dict
    — {"ok", "steered", "approval_id"?/"target"?} shapes.
    """
    instruction = (instruction or "").strip()
    if not instruction:
        return {"ok": False, "error": "Instruction is empty"}

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

    requester_name = origin_session_key
    if requester_contact_id:
        from bob_server.repositories.contacts import ContactRepository
        requester = await ContactRepository(ctx.db).get(requester_contact_id)
        if requester:
            requester_name = requester["name"]

    content = build_wake_content(
        requester_name=requester_name, origin_label=origin_label,
        instruction=instruction)
    metadata = steer_metadata(
        requester_contact_id=requester_contact_id,
        requester_name=requester_name, origin_session_key=origin_session_key)

    from bob_server.services.effects import emit_and_deliver

    # Owner bypass: self-approval is pure friction. Matches on threaded-in
    # contact id, or on the request coming from the owner's own DM (wake-path
    # group turns carry no contact id).
    if requester_contact_id == str(owner["id"]) or origin_session_key == owner_dm:
        result = await emit_and_deliver(
            ctx, kind="conversation_steer",
            idempotency_key=f"conversation_steer:{uuid4()}",
            payload={"target_key": target_key, "content": content,
                     "metadata": {**metadata, "owner_direct": True}})
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error", "steer failed")}
        return {"ok": True, "steered": True, "target": target_label}

    # Dedupe: the approval_request effect key is a fresh uuid per call, so a
    # repeated LLM attempt would otherwise mint a second pending approval
    # (and a second wake once both were approved).
    from bob_server.repositories.approvals import ApprovalRepository

    for row in await ApprovalRepository(ctx.db).pending_of_type(
            APPROVAL_TYPE, entity_id=target_key):
        try:
            proposal = json.loads(row["proposal_data"] or "{}")
        except (TypeError, ValueError):
            continue
        if proposal.get("instruction") == instruction:
            return {"ok": True, "steered": False,
                    "approval_id": row["id"], "duplicate": True}

    # The summary is what _summarize_proposal renders into the owner's wake,
    # so it must carry the target and the exact instruction being approved.
    proposal = {
        "summary": "\n".join([
            f"Target conversation: {target_label} ({target_key})",
            f"Requested by: {requester_name}",
            "",
            "Steering instruction (the target conversation acts on this):",
            instruction,
        ]),
        "target_key": target_key,
        "target_label": target_label,
        "instruction": instruction,
        "content": content,  # the wake's source of truth — delivered verbatim
        "metadata": metadata,
        "origin_session_key": origin_session_key,
        "origin_label": origin_label,
        "requester_contact_id": requester_contact_id,
        "requester_label": requester_name,
    }

    result = await emit_and_deliver(
        ctx, kind="approval_request",
        idempotency_key=f"approval_request:{uuid4()}",
        payload={
            "approval_type": APPROVAL_TYPE,
            "entity_id": target_key,
            "title": f"Steer {target_label}",
            "description": f"Steering request from {requester_name}",
            "proposal": proposal,
            "requested_by": origin_session_key,
            "origin_conversation_id": owner_dm,
        })
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "approval request failed")}
    return {"ok": True, "steered": False, "approval_id": result.get("external_result_id")}


async def on_approved(ctx: AppContext, row: dict[str, Any]) -> None:
    """approval_tools on-approved hook: fire exactly the wake that was
    approved, from the stored proposal.

    Never raises: the decision is already durably recorded, the wake carries
    its own idempotency key, and emit failures are tracked in the effects
    outbox — an exception here would only dead-letter the approval_respond.
    """
    try:
        proposal = json.loads(row.get("proposal_data") or "{}")
    except (TypeError, ValueError):
        logger.error("steer approval %s has unparseable proposal; skipped",
                     row.get("id"))
        return
    target_key = proposal.get("target_key")
    content = proposal.get("content")
    if not target_key or not content:
        logger.error("steer approval %s proposal lacks target_key/content; skipped",
                     row.get("id"))
        return

    # Execution-time binding re-check: a lost binding is terminal — retrying
    # cannot help, and waking a conversation Bob left would violate the
    # membership gate the owner just approved.
    from bob_server.repositories.conversations import ConversationRepository

    binding = await ConversationRepository(ctx.db).active_binding(target_key)
    if not binding or binding.get("endpoint_kind") not in ("dm", "group"):
        logger.error("steer approval %s skipped: no active binding for %s",
                     row.get("id"), target_key)
        return

    from bob_server.services.effects import emit_and_deliver

    result = await emit_and_deliver(
        ctx, kind="conversation_steer",
        idempotency_key=f"steer_wake_approved:{row.get('id')}",
        payload={"target_key": target_key, "content": content,
                 "metadata": {**(proposal.get("metadata") or {}),
                              "approval_id": row.get("id")}})
    if not result.get("ok"):
        logger.error("steer approval %s wake emit failed: %s",
                     row.get("id"), result.get("error"))


# ---------------------------------------------------------------------------
# Registration + tool surface
# ---------------------------------------------------------------------------

def register() -> None:
    """Bind the conversation_steer executor and the on-approved hook
    (idempotent). Importing this module runs it; approval_tools' registration
    tail re-runs it so the pump can never deliver an approval_respond with
    the hook missing — the lazy-registration quirk that affects kinds
    registered in tool-assembly functions cannot bite."""
    from bob_server.services import approval_tools
    from bob_server.services import effects as effects_svc

    async def _exec_steer(ctx: AppContext, payload: dict[str, Any]):
        from bob_server.services.wake_service import wake_conversation

        await wake_conversation(
            ctx, payload["target_key"], payload["content"],
            call_category="steer",
            metadata=payload.get("metadata") or {},
            provenance="steer")
        # No external id: a wake is a local dispatch. When the target's
        # dispatcher is unavailable the message is already stored undispatched
        # and the startup sweep / next inbound dispatches it — that's
        # wake_conversation's crash semantics, not an effect failure.

    effects_svc.register_executor(
        "conversation_steer", _exec_steer, retryable=False)
    approval_tools.register_on_approved(APPROVAL_TYPE, on_approved)


register()


def make_steering_tools(
    ctx: AppContext,
    current_session_key: str,
    requester_contact_id: str | None,
) -> list:
    """steer_conversation — attached on any dispatch a human contact started
    (DM or group, any trust level). Wake-path turns get no steer tool even
    when the route resolves a contact id: the bridge gates on dispatch
    origin, so no nested steering."""

    @tool
    async def steer_conversation(target: str, instruction: str) -> str:
        """Steer a different conversation the user belongs to: it wakes with
        your instruction and composes the message itself, in its own voice.
        THE tool whenever the user asks to tell / inform / update / share /
        send something to another chat or group ("tell the Leeming Boys chat
        about this verdict", "let the AI Doom group know the radio feature
        set is on") or to nudge their own chat ("my chat"). Images and files
        are fine — name a workspace media_path in the instruction and the
        target conversation attaches it with its own send tool. (Bob-initiated
        posts are send_whatsapp_group_message; user-requested messages go
        through THIS tool, no policy flag needed.) target: the group name as
        the user said it, a raw group id, or "my chat" — must be a
        conversation the user belongs to; ambiguous or unknown names return
        candidate groups, so call the tool and read the result rather than
        researching destinations. instruction: self-contained — spell out
        anything the target can't see (what "this verdict" was, which file
        to pull), because the target receives only this text. Owner requests
        steer immediately; anyone else's goes to the owner for approval —
        tell the user which."""
        resolution = await resolve_target(
            ctx, target, requester_contact_id=requester_contact_id)
        if not resolution.get("ok"):
            return json.dumps(resolution)

        origin_label = await session_label(ctx, current_session_key)
        result = await create_request(
            ctx,
            target_key=resolution["target_key"],
            target_label=resolution["target_label"],
            instruction=instruction,
            origin_session_key=current_session_key,
            origin_label=origin_label,
            requester_contact_id=requester_contact_id)
        return json.dumps(result)

    return [steer_conversation]
