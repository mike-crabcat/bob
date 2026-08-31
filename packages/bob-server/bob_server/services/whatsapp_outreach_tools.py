"""WhatsApp outreach tools — proactive messaging and cross-session retrieval."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from bob_server.services.tools import tool
from bob_server.services.openai_service import strip_citation_markers

if TYPE_CHECKING:
    from bob_server.context import AppContext
    from bob_server.services.whatsapp_bridge_service import WhatsAppBridgeService

logger = logging.getLogger(__name__)


def _phone_to_jid(phone_number: str) -> str:
    """Convert a normalized phone number (+CCXXXXXXXXX) to a WhatsApp JID."""
    digits = re.sub(r"\D", "", phone_number)
    return f"{digits}@s.whatsapp.net"


# Proactive group-send rate limiting (Bob Events §1.5): per-group hourly cap.
_GROUP_SEND_HOURS = 1
_GROUP_SEND_MAX_PER_HOUR = 4
_group_send_times: dict[str, list[float]] = {}


def _group_send_allowed(group_key: str, now: float) -> bool:
    import time

    recent = [t for t in _group_send_times.get(group_key, [])
              if now - t < _GROUP_SEND_HOURS * 3600]
    _group_send_times[group_key] = recent
    return len(recent) < _GROUP_SEND_MAX_PER_HOUR


def _record_group_send(group_key: str, now: float) -> None:
    _group_send_times.setdefault(group_key, []).append(now)


async def _deliver_group_send(
    ctx: AppContext,
    *,
    group_key: str,
    group_id: str,
    message: str,
    origin_session_key: str,
    idempotency_key: str,
    provenance: dict[str, Any],
    now: float,
) -> dict[str, Any]:
    """The single group-send primitive: emit the whatsapp_send effect, consume
    the per-group rate-limit slot, mirror the message into the group's history
    (as the bridge does for inbound). Callers own every gate — membership,
    policy, approval. Extra provenance keys ride in the effect payload for the
    dashboard timeline and audit."""
    from bob_server.services.effects import emit_and_deliver

    result = await emit_and_deliver(
        ctx, kind="whatsapp_send",
        idempotency_key=idempotency_key,
        payload={"chat_id": f"{group_id}@g.us", "text": message,
                 "origin_session_key": origin_session_key,
                 **provenance},
    )
    if not result.get("ok"):
        return result
    _record_group_send(group_key, now)

    # Mirror the sent message into the group conversation's history (as
    # the bridge does for inbound), so later prompts see it.
    try:
        from bob_server.services.session_service import SessionService
        await SessionService(ctx).add_message(
            group_key, "assistant", message, channel="whatsapp",
            metadata={"proactive_group_send": True, **provenance},
        )
    except Exception:
        logger.warning("group send: history mirror failed for %s", group_key,
                       exc_info=True)

    return result


def make_group_send_tools(
    ctx: AppContext,
    wa_service: WhatsAppBridgeService,
    current_session_key: str,
) -> list:
    """Proactive group send (Bob Events §1.5).

    ``send_whatsapp_group_message`` — Bob-initiated sends on autonomous
    (wake-path) turns only, gated per group on conversation policy
    ``group_outbound_enabled`` (off by default; the migration-452 policy_json
    machinery), requiring an active group binding (membership) and
    rate-limited per group.

    Human-requested cross-conversation messaging is steering
    (services/steering.py) — the old verbatim relay (request_group_message +
    the group_send approval) was retired with it, and human-started turns
    now carry steer_conversation instead of this tool, so the two paths never
    compete for the same turn."""

    @tool
    async def send_whatsapp_group_message(
        group_id: str,
        message: str,
        goal_id: str = "",
    ) -> str:
        """Send a proactive message to a WhatsApp group you are a member of
        (group_id is the raw group id, no @g.us). For Bob-initiated posts —
        polls, updates, reminders tied to a plan — and only to groups with
        outbound sends enabled; pass goal_id when the send serves a goal so
        it's attributable. User-requested messaging is not this tool: turns
        a human started carry steer_conversation instead (any conversation
        they belong to, no policy flag, can carry images)."""
        import time

        from bob_server.repositories.conversations import ConversationRepository

        if not wa_service.connected:
            return json.dumps({"ok": False, "error": "WhatsApp bridge is not connected"})

        group_key = f"agent:main:whatsapp:group:{group_id}"
        conv_repo = ConversationRepository(ctx.db)

        # Membership: an active group binding must exist for this group.
        binding = await conv_repo.active_binding(group_key)
        if not binding or binding.get("endpoint_kind") != "group":
            return json.dumps({"ok": False,
                               "error": "Not a member of that group (no active group binding)"})

        # Policy gate: disabled by default per group.
        policy = await conv_repo.get_policy(group_key)
        if not policy.get("group_outbound_enabled"):
            return json.dumps({"ok": False,
                               "error": "Group outbound sends are not enabled for this group"})

        now = time.monotonic()
        if not _group_send_allowed(group_key, now):
            return json.dumps({"ok": False, "error": "Group send rate limit reached; retry later"})

        result = await _deliver_group_send(
            ctx, group_key=group_key, group_id=group_id, message=message,
            origin_session_key=current_session_key,
            idempotency_key=f"whatsapp_group_send:{group_key}:{uuid4().hex[:8]}",
            provenance={"goal_id": goal_id or None}, now=now)
        if not result.get("ok"):
            return json.dumps({"ok": False, "error": result.get("error", "delivery failed")})

        return json.dumps({"ok": True, "chat_id": f"{group_id}@g.us"})

    return [send_whatsapp_group_message]


def make_whatsapp_outreach_tools(
    ctx: AppContext,
    wa_service: WhatsAppBridgeService,
    current_session_key: str,
) -> list:
    """Create outreach tools for a WhatsApp DM session.

    Tools: send_whatsapp_to_contact, get_contact_session_messages.
    Injected for any contact in DM sessions.
    """

    @tool
    async def send_whatsapp_to_contact(
        contact_id: str,
        message: str,
        objective: str,
        media_path: str = "",
        parent_goal_id: str = "",
    ) -> str:
        """Send a WhatsApp message to a contact (not the current chat).
        The 'objective' describes the specific outcome you need from this conversation,
        e.g. "Find out if John can meet on Thursday and what time works." The target
        session will be instructed to work toward this objective and report back when complete.
        Optionally attach an image or media file by providing media_path.
        Pass parent_goal_id to roll this outreach up into a plan's child goal
        (e.g. a time-negotiation fan-out) instead of waking you directly."""
        parent_goal = parent_goal_id.strip() or None
        from bob_server.services.session_service import SessionService

        db = ctx.db

        # Look up contact
        from bob_server.repositories.contacts import ContactRepository
        contact = await ContactRepository(db).get(contact_id)
        if contact is None:
            return json.dumps({"ok": False, "error": "Contact not found"})

        phone = contact["phone_number"]
        if not phone:
            return json.dumps({"ok": False, "error": "Contact has no phone number"})

        # Outbound-only contacts (agent-created for a call, or operator-restricted)
        # have their inbound DMs dropped by WhatsAppInboundPolicy — outreach to
        # them would invite a reply that can never arrive, leaving a goal
        # waiting on an answer Bob will never see. Fail loudly instead.
        if not bool(contact.get("allow_inbound_dm", 1)):
            return json.dumps({
                "ok": False,
                "error": (
                    f"Contact {contact['name']} is outbound-only "
                    "(allow_inbound_dm=0): their replies would be silently dropped. "
                    "Ask the operator to enable inbound DMs for this contact "
                    "in the dashboard before messaging them."
                ),
            })

        # Check bridge connectivity
        if not wa_service.connected:
            return json.dumps({"ok": False, "error": "WhatsApp bridge is not connected"})

        # Convert phone to JID and send
        jid = _phone_to_jid(phone)
        if media_path:
            from bob_server.services.whatsapp_bridge_service import _prepare_media
            workspace = ctx.settings.harness.workspace_dir.expanduser().resolve()
            resolved = (workspace / media_path).resolve()
            if not str(resolved).startswith(str(workspace)):
                return json.dumps({"ok": False, "error": "Media path escapes workspace"})
            if not resolved.is_file():
                return json.dumps({"ok": False, "error": f"Media file not found: {media_path}"})
            prepared = await _prepare_media(str(resolved))
            if prepared is None:
                return json.dumps({"ok": False, "error": "Failed to prepare media for sending"})
            request_id = await wa_service.send_media(jid, prepared, caption=message)
        else:
            request_id = await wa_service.send_message(jid, message)

        # Derive session key for the target contact
        phone_digits = re.sub(r"\D", "", phone)
        target_session_key = f"agent:main:whatsapp:dm:{phone_digits}"

        # Derive requestor name from current session context
        requestor_name = "the agent"
        from bob_server.repositories.conversations import ConversationRepository
        current_route = await ConversationRepository(db).route_for(current_session_key)
        if current_route and current_route.get("contact_id"):
            from bob_server.repositories.contacts import ContactRepository
            requestor = await ContactRepository(db).get(current_route["contact_id"])
            if requestor:
                requestor_name = requestor["name"]

        # Bob3 Phase V + Increment 3: outreach state lives ON the goal
        # (strategy carries requestor/message for the target-side prompt).
        # A 24h deadline wakeup resurfaces unanswered outreach in the origin
        # conversation. Bob Events: a parented outreach inherits the parent's
        # entity refs so the target DM's §2.0 candidate seeding offers the
        # plan's entities — otherwise the extractor there mints duplicate
        # slugs and the reply never ref-matches back into the plan.
        goal_id = None
        try:
            from datetime import datetime, timedelta, timezone

            from bob_server.services.goal_service import create_goal
            phone_digits_g = re.sub(r"\D", "", phone)
            strategy = {"requestor": requestor_name, "message": message}
            if parent_goal:
                from bob_server.repositories.goals import GoalRepository
                from bob_server.services.goal_state_service import parse_strategy
                parent = await GoalRepository(db).get(parent_goal)
                if parent is not None:
                    refs = parse_strategy(parent).refs.entities
                    if refs:
                        strategy["refs"] = {"entities": list(refs), "claims": []}
            goal = await create_goal(
                ctx,
                conversation_id=f"agent:main:whatsapp:dm:{phone_digits_g}",
                objective=objective,
                origin_conversation_id=current_session_key,
                kind="outreach",
                strategy=strategy,
                deadline=(datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
                parent_goal_id=parent_goal,
            )
            goal_id = goal["id"]
        except Exception:
            logger.warning("failed to create outreach goal", exc_info=True)

        # Ensure the target DM binding exists (no outreach state on it).
        from bob_server.repositories.conversations import ConversationRepository
        await ConversationRepository(db).register_endpoint(
            target_session_key, endpoint_kind="dm", contact_id=str(contact["id"]))

        # Store the outreach message in target session history as assistant (bob sent it)
        session_service = SessionService(ctx)
        await session_service.add_message(
            target_session_key, "assistant", message,
            channel="whatsapp",
            metadata={"outreach": True, "objective": objective, "requestor": requestor_name},
        )

        # Upsert the contact as a participant in the target session
        from bob_server.services.base import utcnow
        now_iso = utcnow().isoformat()
        from bob_server.repositories.participants import ParticipantRepository
        await ParticipantRepository(db).upsert(
            target_session_key, phone,
            display_name=contact["name"], contact_id=contact["id"],
            is_trusted=True, now_iso=now_iso)

        logger.info(
            "Outreach sent to %s (%s) session=%s request=%s objective=%s",
            contact["name"], phone, target_session_key, request_id, objective[:80],
        )

        # Log under source session so it shows as bob-initiated
        from bob_server.services.llm_dispatch import _record_log
        await _record_log(
            db,
            provider="outreach",
            model="",
            call_category="whatsapp_outreach",
            session_key=current_session_key,
            user_message=f"Reach out to {contact['name']}: {objective}",
            response_text=message,
            status="completed",
            contact_id=contact["id"],
        )
        # Also log under target session so it surfaces in the dashboard
        await _record_log(
            db,
            provider="outreach",
            model="",
            call_category="whatsapp_outreach",
            session_key=target_session_key,
            user_message=f"[Outreach initiated — requested by {requestor_name}] {objective}",
            response_text=message,
            status="completed",
            contact_id=contact["id"],
        )

        return json.dumps({
            "ok": True,
            "contact_name": contact["name"],
            "request_id": request_id,
        })

    @tool
    async def get_contact_session_messages(
        contact_name: str,
        limit: int = 10,
    ) -> str:
        """Retrieve recent messages from a contact's WhatsApp session.
        Use this to check if a contact has replied to an outreach message."""
        from bob_server.services.session_service import SessionService

        db = ctx.db

        # Look up contact by name
        from bob_server.repositories.contacts import ContactRepository
        contact = await ContactRepository(db).search_by_name(f"%{contact_name}%")
        if contact is None:
            return json.dumps({"ok": False, "error": f"No contact found matching '{contact_name}'"})

        phone_digits = re.sub(r"\D", "", contact["phone_number"])
        target_session_key = f"agent:main:whatsapp:dm:{phone_digits}"

        session_service = SessionService(ctx)
        messages = await session_service.get_messages(target_session_key, limit=limit)

        if not messages:
            return json.dumps({
                "ok": True,
                "contact_name": contact["name"],
                "messages": [],
                "note": "No messages found in this session yet.",
            })

        return json.dumps({
            "ok": True,
            "contact_name": contact["name"],
            "messages": [
                {
                    "role": m.role,
                    "content": m.content,
                    "channel": m.channel,
                    "created_at": m.created_at,
                }
                for m in messages
            ],
        })

    return [send_whatsapp_to_contact, get_contact_session_messages]


def _session_key_to_chat_id(session_key: str) -> str | None:
    """Derive a WhatsApp chat_id (JID) from a session key."""
    parts = session_key.split(":")
    if len(parts) < 5 or parts[2] != "whatsapp":
        return None
    kind = parts[3]
    ident = parts[4]
    if kind == "dm":
        return f"{ident}@s.whatsapp.net"
    if kind == "group":
        return f"{ident}@g.us"
    return None


def make_outreach_reply_tools(
    ctx: AppContext,
    wa_service: WhatsAppBridgeService,
    current_session_key: str,
) -> list:
    """Create the finish_outreach tool for an active outreach target session.

    When called, dispatches an LLM call in the source session to receive the result.
    """

    @tool
    async def finish_outreach(result: str) -> str:
        """Complete the active outreach request and relay the result back.
        Call when you have achieved the objective or obtained the requested information.
        The result will be dispatched to the originating session, which will decide
        how to handle it (potentially messaging the requesting contact)."""
        from bob_server.services.goal_service import settle_goal
        from bob_server.services.wake_service import wake_conversation

        db = ctx.db

        # The active outreach goal held by this conversation IS the state.
        from bob_server.repositories.goals import GoalRepository
        goal = await GoalRepository(db).active_outreach(current_session_key)
        if not goal or not goal["origin_conversation_id"]:
            return json.dumps({"ok": False, "error": "No active outreach to report"})

        origin_session_key = goal["origin_conversation_id"]
        objective = goal["objective"] or "unknown"
        goal_id = goal["id"]
        try:
            strategy = json.loads(goal["strategy_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            strategy = {}
        requestor = strategy.get("requestor", "unknown")

        # Look up target contact name for context
        from bob_server.repositories.conversations import ConversationRepository
        target_contact_name = await ConversationRepository(db).contact_name_for(
            current_session_key) or "unknown"

        result_content = (
            f"## Outreach Result\n"
            f"Contact: {target_contact_name}\n"
            f"Objective: {objective}\n"
            f"Requested by: {requestor}\n\n"
            f"{result}"
        )

        # Bob3 Phase V: settling the goal wakes the origin conversation with
        # the result (any channel). Legacy outreach without a goal wakes
        # the origin directly.
        settled = False
        if goal_id:
            try:
                settled = await settle_goal(
                    ctx, goal_id, status="completed", result=result_content,
                )
            except Exception:
                logger.warning("failed to settle outreach goal %s", goal_id, exc_info=True)
        if not settled:
            await wake_conversation(
                ctx, origin_session_key, result_content,
                call_category="outreach_result",
                metadata={"outreach_result": True, "source_session": current_session_key},
            )

        logger.info(
            "Outreach finished from %s to %s, result relayed via wake path",
            current_session_key, origin_session_key,
        )

        return json.dumps({
            "ok": True,
            "dispatched_to": origin_session_key,
        })

    return [finish_outreach]
