"""WhatsApp emoji-reaction handlers (2026-09-14).

Mixin: these methods rely on the host class providing ``self.ctx``,
``self.db``, ``self._get_settings``, ``self._send_ack``,
``self._resolve_whatsapp_sender``, ``self._derive_whatsapp_session``,
``self._register_whatsapp_participant`` and
``self._build_inbound_dispatch_spec``.

Inbound reactions are stored as self-describing user rows (provenance
``wa_reaction``) and render inline in replayed history. A DM reaction that
targets one of Bob's own messages (an assistant row, resolvable via its
stamped WhatsApp id) wakes a turn — everywhere else reactions are passive:
stored, rendered, never dispatched. Content deliberately avoids a
``'[reaction] '`` prefix so the attention probe's clip-cooldown query
(``probe_reaction_within`` matches assistant rows by that prefix) can't
collide.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# The WhatsApp-standard reaction set (plus 🔥). The outbound tool exposes
# this as a JSON-schema enum AND re-validates at write time — models invent
# emoji otherwise.
REACTION_EMOJI_ALLOWLIST = ("👍", "❤️", "😂", "😮", "😢", "🙏", "🔥")

# System note for reaction-wake turns: a reaction is a light engagement
# signal, so silence is the designed majority outcome (the NO_REPLY path is
# what "he saw it" looks like). Rides the inbound spec builder via
# extra_system_note.
REACTION_HANDLING_NOTE = (
    "## WhatsApp Reaction\n"
    "This turn was triggered by a WhatsApp reaction (e.g. a 👍) to one of "
    "your messages, shown in the new-message trailer as a "
    "`[reaction … to your message: …]` line. A reaction is a light "
    "engagement signal — the normal response is to acknowledge it "
    "internally and stay silent: call send_whatsapp_message with the text "
    "NO_REPLY. Only reply if the reaction clearly invites one (for "
    "example, it answers a question you asked, or the conversation was "
    "left waiting on it)."
)


def _snippet_of(row: dict[str, Any] | None) -> str | None:
    """First line of the target row's content, capped at 80 chars. Media
    rows already store their stub ([Image], [GIF], …) as content when
    uncaptioned, so the content ladder covers them."""
    if not row:
        return None
    content = (row.get("content") or "").strip()
    if not content:
        return None
    first = content.splitlines()[0].strip()
    if len(first) > 80:
        first = first[:80] + "…"
    return first or None


class ReactionsMixin:
    """Inbound emoji-reaction storage, rendering rows, and the wake rule."""

    _REACTION_TARGETS = ("last_user_message", "own_last_message")

    async def _handle_incoming_reaction(self, payload: dict[str, Any]) -> None:
        from server.services.session_service import SessionService

        settings = self._get_settings()
        reaction_wa_id = payload.get("whatsapp_message_id", "")
        chat_id = payload.get("chat_id", "")
        chat_kind = payload.get("chat_kind", "dm")
        sender_jid = payload.get("sender_jid", "")
        sender_name = payload.get("sender_name", "")
        target_message_id = payload.get("target_message_id", "")
        emoji = payload.get("emoji", "")

        # Always ack — even when dropped below — or the bridge's durable
        # inQ redelivers this reaction forever.
        await self._send_ack(reaction_wa_id)

        if not settings.openai.enabled or not settings.whatsapp_bridge.inbound_reactions_enabled:
            logger.info(
                "reaction not handled (disabled): chat=%s emoji=%s target=%s",
                chat_id, emoji, target_message_id)
            return
        if not target_message_id:
            logger.warning("reaction without target id, dropping: chat=%s", chat_id)
            return

        logger.info(
            "incoming whatsapp reaction: chat_id=%s chat_kind=%s sender_jid=%s "
            "sender_name=%s emoji=%s target=%s",
            chat_id, chat_kind, sender_jid, sender_name, emoji, target_message_id)

        resolved = await self._resolve_whatsapp_sender(
            chat_id, chat_kind, sender_jid, sender_name,
            preview=f"[reaction {emoji}]")
        if resolved is None:
            return
        phone_number, contact_id, is_trusted = resolved

        binding_key, session_key = await self._derive_whatsapp_session(
            chat_id, chat_kind, sender_jid, phone_number)
        await self._register_whatsapp_participant(
            session_key, chat_id, chat_kind, phone_number, sender_name,
            contact_id, is_trusted)

        # Target resolution: the reacted-to message must be a stored row in
        # this conversation. Unresolvable (pre-feature messages, ids lost to
        # a restart) → passive with an "an earlier message" placeholder —
        # the wake rule stays conservative, so only missed wakes, never
        # false ones.
        from server.repositories.history import HistoryRepository
        target_row = await HistoryRepository(self.db).message_by_wa_id(
            session_key, target_message_id)
        target_role = target_row.get("role") if target_row else None
        snippet = _snippet_of(target_row)

        wake = chat_kind == "dm" and target_role == "assistant"

        if emoji:
            if snippet:
                content = (f'[reaction {emoji} to your message: "{snippet}"]'
                           if wake else f'[reaction {emoji} to: "{snippet}"]')
            else:
                content = f"[reaction {emoji} to an earlier message]"
        else:
            content = (f'[reaction removed from: "{snippet}"]' if snippet
                       else "[reaction removed from an earlier message]")

        metadata = {
            "wa_message_id": reaction_wa_id or None,
            "reaction": {
                "emoji": emoji,
                "target_wa_message_id": target_message_id,
                "target_snippet": snippet,
                "target_role": target_role,
                "sender_jid": sender_jid or None,
                "sender_name": sender_name or None,
            },
        }

        # Dedupe-first, then store, in one transaction: the event log's
        # (source=whatsapp, external_id=<reaction wa id>) accept-once
        # guarantees a bridge redelivery can't double-store.
        from server.repositories import Event, EventLogRepository

        stored_msg_id: str | None = None
        async with self.db.transaction() as txn:
            ingress_event_id = await EventLogRepository(self.db).append(
                Event(
                    event_type="message.reaction",
                    binding_key=binding_key,
                    conversation_id=session_key,
                    source="whatsapp",
                    external_id=reaction_wa_id or None,
                    payload={
                        "emoji": emoji,
                        "chat_kind": chat_kind,
                        "target_message_id": target_message_id,
                        "sender_name": sender_name,
                    },
                ), txn=txn)
            if ingress_event_id is None:
                logger.info(
                    "duplicate reaction ignored (bridge redelivery): id=%s chat=%s",
                    reaction_wa_id, chat_id)
                return
            stored_msg_id = await SessionService(self.ctx).add_message(
                session_key, "user", content,
                channel="whatsapp", sender_id=contact_id,
                # Wake rows must be claimable (dispatched=0) or the runner
                # has nothing to claim; passive rows are dispatched=1 so
                # they never surface as awaiting-reply.
                dispatched=0 if wake else 1,
                provenance="wa_reaction", metadata=metadata, txn=txn)

        if not wake:
            # Passive by design: rendered in the next turn's history, no
            # dispatch, never in the awaiting-reply trailer.
            return

        # Occupancy: a live call defers the wake to the post-call drain —
        # the row is already dispatched=0, so the drain's wake_session
        # picks it up with whatever else queued.
        from server.services import occupancy
        if occupancy.is_live(session_key):
            occupancy.defer(session_key)
            logger.info("occupancy: call live on %s — reaction wake deferred",
                        session_key)
            return

        await self._dispatch_reaction_turn(
            session_key=session_key, chat_id=chat_id, contact_id=contact_id,
            is_trusted=is_trusted, sender_name=sender_name,
            reaction_meta=metadata["reaction"])

    async def _dispatch_reaction_turn(
        self, *, session_key: str, chat_id: str,
        contact_id: str | None, is_trusted: bool, sender_name: str,
        reaction_meta: dict[str, Any],
    ) -> None:
        """Direct dispatch (no attention coordinator): a DM reaction to
        Bob's own message IS the address signal — tiering would only mute
        it. The handling note makes NO_REPLY the default outcome."""
        from server.services.dispatch_runner import DispatchRunner

        try:
            spec = await self._build_inbound_dispatch_spec(
                session_key=session_key,
                chat_id=chat_id,
                chat_kind="dm",
                contact_id=contact_id,
                is_trusted=is_trusted,
                human_initiated=True,
                sender_name=sender_name,
                text_preview=f"[reaction {reaction_meta.get('emoji', '')}]",
                extra_system_note=REACTION_HANDLING_NOTE)

            async def _run() -> str:
                return await DispatchRunner(self.ctx).run(spec)

            asyncio.create_task(_run(), name=f"wa_reaction_wake:{session_key}")
            logger.info("dispatching whatsapp reaction wake session=%s", session_key)
        except Exception:
            logger.exception("reaction wake dispatch failed (session=%s)", session_key)


# ---------------------------------------------------------------- outbound


def _wa_ids_of(row: dict[str, Any]) -> list[str]:
    """WhatsApp message ids referenced by a history row's metadata — the
    inbound id (user rows) or the send ids (assistant rows)."""
    raw = row.get("metadata")
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(raw, dict):
        return []
    ids = [raw["wa_message_id"]] if raw.get("wa_message_id") else []
    sends = raw.get("sends")
    if isinstance(sends, list):
        ids += [s["wa_message_id"] for s in sends
                if isinstance(s, dict) and s.get("wa_message_id")]
    return ids


async def _resolve_reaction_target(
    db: Any, session_key: str, chat_id: str, chat_kind: str, target: str,
) -> tuple[str, str, str | None] | str:
    """Resolve the react tool's target enum to (wa_message_id,
    target_sender_jid, snippet), or an error string. The enum is the whole
    contract — the model never sees raw WhatsApp ids in history, so a free
    id parameter would only hallucinate."""
    from server.repositories.history import HistoryRepository

    rows = await HistoryRepository(db).recent_dialogue(session_key, limit=50)
    want_role = "user" if target == "last_user_message" else "assistant"
    for row in reversed(rows):  # newest-first
        if row.get("role") != want_role:
            continue
        wa_ids = _wa_ids_of(row)
        if not wa_ids:
            continue
        wa_id = wa_ids[-1]
        snippet = _snippet_of(row)
        if want_role == "assistant":
            # Reacting to his own message: empty author JID → FromMe key.
            return wa_id, "", snippet
        if chat_kind == "dm":
            return wa_id, chat_id, snippet
        # Group: the target's author is the participant who sent the row —
        # resolve contact phone → JID (bridge ResolveLID normalises).
        sender_jid = ""
        sender_id = row.get("sender_id")
        if sender_id:
            from server.repositories.contacts import ContactRepository
            contact = await ContactRepository(db).get(str(sender_id))
            if contact and contact.get("phone_number"):
                digits = re.sub(r"\D", "", str(contact["phone_number"]))
                if digits:
                    sender_jid = f"{digits}@s.whatsapp.net"
        if not sender_jid:
            return ("Error: could not resolve who sent that message — "
                    "not reacting to an ambiguous target.")
        return wa_id, sender_jid, snippet
    if want_role == "user":
        return ("Error: no recent inbound message in this conversation can "
                "be reacted to.")
    return ("Error: no recent message of yours in this conversation can be "
            "reacted to.")


def make_react_tool(ctx: Any, wa_service: Any, session_key: str, chat_id: str,
                    chat_kind: str, dispatch_id: str, send_seq: list) -> Any:
    """The react_whatsapp_message reply tool: posts a single-emoji WhatsApp
    reaction inside the current conversation. In-conversation by
    construction (same risk class as the send reply tool), so it attaches
    wherever send_whatsapp_message does, flag-gated."""
    from server.services.effects import emit_and_deliver
    from server.services.tools import Tool

    reacted: set[tuple[str, str]] = set()

    async def _react_whatsapp_message(emoji: str = "",
                                      target: str = "last_user_message") -> str:
        settings = wa_service._get_settings()
        if not settings.whatsapp_bridge.outbound_reactions_enabled:
            return "Error: reactions are disabled."
        emoji = (emoji or "").strip()
        if emoji not in REACTION_EMOJI_ALLOWLIST:
            return ("Error: not sent — pick exactly one emoji from: "
                    + " ".join(REACTION_EMOJI_ALLOWLIST))
        if target not in ReactionsMixin._REACTION_TARGETS:
            return ("Error: not sent — target must be one of: "
                    + ", ".join(ReactionsMixin._REACTION_TARGETS))
        resolved = await _resolve_reaction_target(
            wa_service.db, session_key, chat_id, chat_kind, target)
        if isinstance(resolved, str):
            return resolved
        target_wa_id, target_sender_jid, snippet = resolved
        if (target_wa_id, emoji) in reacted:
            return (f"Error: not sent — you already reacted {emoji} to that "
                    "message this turn.")
        reacted.add((target_wa_id, emoji))
        seq = send_seq[0]
        send_seq[0] += 1
        result = await emit_and_deliver(
            ctx, kind="whatsapp_react",
            idempotency_key=f"whatsapp_react:{dispatch_id}:{seq}",
            payload={"chat_id": chat_id,
                     "target_message_id": target_wa_id,
                     "target_sender_jid": target_sender_jid,
                     "emoji": emoji})
        if not result.get("ok"):
            return f"Error sending reaction: {result.get('error', 'delivery failed')}"
        # Bob's own reactions belong in replayed history (else later turns
        # don't know he reacted) — direct row, not sent_texts, so a
        # NO_REPLY+react turn still records it. Best-effort.
        try:
            from server.services.session_service import SessionService
            content = (f'[you reacted {emoji} to: "{snippet}"]' if snippet
                       else f"[you reacted {emoji}]")
            await SessionService(ctx).add_message(
                session_key, "assistant", content, channel="whatsapp",
                provenance="wa_reaction_sent", dispatched=1,
                metadata={"reaction": {
                    "emoji": emoji, "target_wa_message_id": target_wa_id}})
        except Exception:
            logger.warning("reaction history write failed (session=%s)",
                           session_key, exc_info=True)
        return f"Reacted {emoji} (request_id={result.get('external_result_id')})"

    return Tool(
        name="react_whatsapp_message",
        description=(
            "React to a message in the current WhatsApp conversation with a "
            "single emoji (e.g. a thumbs-up acknowledgement). This is "
            "lightweight — prefer it over a text reply when a message just "
            "needs acknowledgement. It does NOT replace send_whatsapp_message "
            "for actual replies."
        ),
        parameters={
            "emoji": {"type": "string", "enum": list(REACTION_EMOJI_ALLOWLIST),
                      "description": "Exactly one emoji from this list."},
            "target": {"type": "string",
                       "enum": list(ReactionsMixin._REACTION_TARGETS),
                       "description": "Which message to react to: the user's "
                                      "most recent message (default), or your "
                                      "own most recent message."},
        },
        required=[],
        handler=_react_whatsapp_message,
    )
