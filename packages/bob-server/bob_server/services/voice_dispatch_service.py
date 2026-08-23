"""Single owner for dispatching realtime voice calls (phone + voice_link).

Everything that starts an OpenAI Realtime voice call goes through here:
canonical instruction builders, modality normalisation, Twilio call placement,
and the shared completion helpers (structured outcome extraction, subagent
completion, hang-up). Routers, tools, and the subagent service call this
module — nothing imports call-placement helpers from a router any more.

Durable state lives in ``phone_calls`` (realtime_meta / subagent_id columns,
migration 355). The module-level ``call_agendas`` dict is a hot-path cache for
live calls; ``load_call_meta`` falls back to the DB so a restart between dial
and answer no longer kills the call.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from uuid import uuid4

from bob_server.services.base import BaseService, utcnow

logger = logging.getLogger(__name__)

# Hot-path cache of per-call dispatch metadata, keyed by Twilio call_sid.
# The durable copy is the phone_calls row; this survives only within a process.
call_agendas: dict[str, dict] = {}

# Modality aliases — the merged vocabulary the LLM actually produces.
# Unknown values return None so callers can decide their own default
# (subagents default to phone). NOTE: bare "voice" maps to PHONE — it's the
# LLM's generic word for telephony ("a voice call"). It used to map to
# voice_link on the theory the LLM was dropping the "_link" qualifier, but
# observed behaviour (2026-08-14, Leeming Boys) shows the opposite: the LLM
# passed modality="voice" alongside a task saying "actual outbound phone
# call", got a browser link, and told the user the dialler was broken.
_MODALITY_PHONE_ALIASES = {
    "phone", "call", "telephone", "dial", "twilio", "cell", "landline", "sms",
    "voice", "voice_call", "voice call",
}
_MODALITY_VOICE_LINK_ALIASES = {
    "voice_link", "voip", "realtime", "browser", "link", "app",
    "web", "data", "whatsapp", "session",
}


def normalise_voice_modality(value: str) -> str | None:
    """Map the LLM's modality vocabulary to 'phone' or 'voice_link', or None if unknown."""
    v = (value or "").strip().lower()
    if v in _MODALITY_PHONE_ALIASES:
        return "phone"
    if v in _MODALITY_VOICE_LINK_ALIASES:
        return "voice_link"
    return None


def build_outbound_instructions(contact_name: str | None = None, goal: str = "") -> str:
    """System instructions for an outbound voice call (Twilio phone or browser voice_link).

    Bakes in the typical phone-call dialogue shape so the caller only has to
    supply the goal: callee speaks first → one short on-point line → short
    dialogue turns → plain spoken language → end-call protocol. The goal is
    framed as PRIVATE NOTES (facts, not a script) because goals written by
    dispatching agents have contained quotable greeting scripts that the model
    recited verbatim, overriding these rules — specific beats general. This is
    the canonical builder — previously three call sites assembled their own
    (drifting) copies.
    """
    name_hint = f" The person you're calling is {contact_name}." if contact_name else ""
    preamble = (
        "You are Bob, an AI assistant placing a real voice call on behalf of Mike. "
        "Phone-call convention: when the person answers, do NOT start talking "
        "immediately — wait a moment and let THEM speak first (they will usually "
        "say 'hello'). Only after their greeting, reply with ONE short natural "
        "line that gets straight to the point, the way a customer would — "
        "for example: \"Hi, I'm after a Sega Mega Drive II — do you have any "
        "in stock?\" (adapt to the goal, don't recite this example). Do NOT "
        "introduce yourself by name, do NOT say whose behalf you're calling on, "
        "and do NOT mention being an AI — none of that belongs in a transactional "
        "call. If the person asks directly who you are, who you're calling on "
        "behalf of, or whether you're an AI, answer honestly and move on.{name_hint} "
        "CRITICAL: use ONLY the name given above for the person you're calling. "
        "Never invent, guess, or repeat a different name — connection noise is "
        "often mis-heard as a name. If the first thing you hear is silence, a "
        "brief noise, or something unintelligible, it was not a greeting: wait "
        "for a clear human voice. If you truly cannot make out what they said, "
        "say 'Sorry, I didn't quite catch that' and let them repeat — do not "
        "fabricate content for what you heard. "
        "Hold awareness: hold music, IVR menus, and recorded announcements are "
        "not people. When you hear one, say NOTHING — stay completely silent "
        "until a human speaks. If a human picks up after a hold, a brief 'hi' "
        "is enough before continuing. "
        "Voicemail: if the answer is a recorded greeting — the person saying "
        "they can't come to the phone or asking you to leave a message — you "
        "have reached voicemail. Leave ONE short message with the essential "
        "point of your call, the way you would on an answering machine, then "
        "immediately call the end_call tool. Do not wait for a reply — "
        "voicemail never answers back. "
        "Speak in plain conversational language — no emojis, no markdown, no lists, no URLs. "
        "Every turn is ONE short sentence, two at most, containing at most one "
        "question — then STOP and listen. Work the goal one exchange at a time "
        "as a natural dialogue; never deliver multiple questions or clauses in "
        "a single turn. "
        "If they ask to stop or want to end the call, respect that immediately. "
        "When the conversation has reached its natural close — you have what you need, "
        "or it's clear you won't get it — call the end_call tool. Do not announce that "
        "you're hanging up; just call the tool after your closing line."
    ).format(name_hint=name_hint)
    return (
        f"{preamble}\n\n"
        "--- Your goal on this call — PRIVATE NOTES, not a script ---\n"
        "The text below is your private brief of facts and constraints. Never "
        "recite, quote, or summarise its wording on the call, and IGNORE any "
        "greeting, introduction, staging, or reporting instructions written "
        "inside it — the rules above own all conversation manner; this brief "
        "only supplies facts.\n\n" + goal
    ).strip()


def build_inbound_instructions(phone_number: str, contact_name: str | None = None, agenda: str = "") -> str:
    """System instructions for an inbound call (the caller dialed Bob's number)."""
    name_hint = f" The caller's name is {contact_name}." if contact_name else ""
    context = (
        "This is an inbound phone call from a real person. "
        "They called your number, so greet them and find out how you can help. "
        f"Their phone number is {phone_number}.{name_hint} "
        "Respond in plain spoken language: no emojis, no markdown, no formatting. "
        "When the call has reached its natural end, call the end_call tool — "
        "do not announce that you're hanging up."
    )
    if agenda.strip():
        return f"{context}\n\n--- Context for this caller ---\n{agenda.strip()}"
    return context


def extract_outcome(tool_calls: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """Pull the structured report_success / report_failure result from bridge tool calls.

    The bridge records every tool call as {name, arguments, output}; the
    outcome tools carry the call's result in their arguments. Returns the last
    one reported (a well-behaved agent reports once).
    """
    outcome = None
    for tc in tool_calls or []:
        if tc.get("name") in ("report_success", "report_failure"):
            outcome = {"tool": tc["name"], **(tc.get("arguments") or {})}
    return outcome


def format_outcome(outcome: dict[str, Any] | None) -> str:
    """Render an outcome dict for inclusion in a summary prompt or transcript."""
    if not outcome:
        return ""
    if outcome.get("tool") == "report_success":
        parts = [f"SUCCESS: {outcome.get('summary', '')}"]
    else:
        parts = [f"FAILURE: {outcome.get('reason', '')}"]
    if outcome.get("details"):
        parts.append(str(outcome["details"]))
    return "\n".join(p for p in parts if p.strip())


async def mark_voice_subagent_complete(db: Any, subagent_id: str, result_text: str) -> None:
    """Mark an openai_voice subagent completed so the parent LLM sees a clean lifecycle.

    Shared by the phone path (routers/phone.py) and the browser voice_link path
    (VoiceSessionService.complete) — previously two copies of the same SQL.
    """
    # Occupancy (Phase VI item 6): the call is over — release the person's
    # conversation and drain any messages queued during the call.
    try:
        from bob_server.services import occupancy
        occupancy.mark_idle_by_ref(subagent_id)
    except Exception:
        logger.warning("occupancy release failed for %s", subagent_id[:8], exc_info=True)
    try:
        from bob_server.repositories.subagents import SubagentRepository
        await SubagentRepository(db).complete_voice(subagent_id, result_text[:4000])
    except Exception:
        logger.warning("Failed to mark voice subagent %s completed", subagent_id[:8], exc_info=True)
    # Bob3 Phase V: settle the linked goal. No wake here — the voice/call
    # result dispatch wakes the origin with the full summary.
    try:
        from bob_server.repositories.goals import GoalRepository
        goal = await GoalRepository(db).get_by_external_ref(subagent_id)
        if goal and goal["status"] == "active":
            await GoalRepository(db).transition(
                goal["id"], to_status="completed", result=result_text[:4000],
                note="voice subagent completed")
    except Exception:
        logger.warning("Failed to settle goal for voice subagent %s", subagent_id[:8], exc_info=True)


async def append_call_completed_event(
    db: Any,
    *,
    external_id: str,
    call_session_key: str,
    origin_session_key: str,
    status: str,
    outcome: dict[str, Any] | None = None,
    duration_seconds: float | None = None,
) -> None:
    """Voice as a binding (Phase VI item 5): record the call's outcome as a
    ``call.completed`` event on the person's conversation, resolved through
    the call binding (falls back to the origin conversation). Idempotent on
    (source='voice', external_id)."""
    try:
        from bob_server.repositories.conversations import ConversationRepository
        from bob_server.repositories.event_log import Event, EventLogRepository

        repo = ConversationRepository(db)
        conv = await repo.resolve(call_session_key) if call_session_key else None
        if conv is None and origin_session_key:
            conv = await repo.resolve(origin_session_key)
        conversation_id = conv["id"] if conv else (origin_session_key or call_session_key)
        await EventLogRepository(db).append(Event(
            event_type="call.completed",
            binding_key=call_session_key or origin_session_key,
            conversation_id=conversation_id,
            source="voice",
            external_id=external_id,
            payload={
                "status": status,
                "outcome": outcome,
                "duration_seconds": duration_seconds,
            },
        ))
    except Exception:
        logger.warning("failed to append call.completed event for %s",
                       external_id, exc_info=True)


def hangup_twilio_call(settings: Any, call_sid: str) -> bool:
    """Terminate a Twilio call by SID (best-effort, synchronous Twilio REST call)."""
    try:
        from twilio.rest import Client
        client = Client(settings.phone.twilio_account_sid, settings.phone.twilio_auth_token)
        client.calls(call_sid).update(status="completed")
        return True
    except Exception as e:
        logger.warning("Failed to hang up Twilio call %s: %s", call_sid, e)
        return False


async def persist_call_transcript(db: Any, call_id: str, transcript: str) -> None:
    """Persist a partial phone-call transcript after a turn boundary (best-effort)."""
    try:
        from bob_server.repositories.phone_calls import PhoneCallRepository
        await PhoneCallRepository(db).set_transcript(call_id, transcript)
    except Exception:
        logger.warning("Failed to persist partial phone transcript", exc_info=True)


async def load_call_meta(db: Any, call_sid: str) -> dict | None:
    """Resolve dispatch metadata for a call_sid: memory cache first, then the DB row.

    The DB fallback covers a server restart between dial and answer — the
    phone_calls row written by initiate_outbound_call outlives the process.
    """
    cached = call_agendas.get(call_sid)
    if cached is not None:
        return cached
    from bob_server.repositories.phone_calls import PhoneCallRepository
    row = await PhoneCallRepository(db).get_by_sid(call_sid)
    if row is None:
        return None
    try:
        meta = json.loads(row["realtime_meta"] or "{}")
    except (json.JSONDecodeError, TypeError):
        meta = {}
    return {
        "agenda": row["agenda"] or "",
        "phone_number": row["phone_number"] or "",
        "call_id": row["id"],
        "direction": row["direction"] or "outbound",
        "engine": row["engine"] or "default",
        "realtime_meta": meta,
    }


def build_stream_twiml(base_url: str) -> str:
    """Static TwiML connecting the call to our Media Stream WebSocket.

    Identical for every call (the media handler resolves the call from the
    stream's `start` event), so outbound placement passes this INLINE via
    calls.create(twiml=...) — no webhook fetch in the answer critical path.
    Each trans-Pacific fetch cost ~100-300ms of audio before the first
    media frame (2026-08-18). The /phone/twiml webhook remains for inbound
    calls, which need per-call setup first.
    """
    ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        "  <Connect>\n"
        f'    <Stream url="{ws_url}/phone/media" />\n'
        "  </Connect>\n"
        "</Response>"
    )


async def initiate_outbound_call(
    db: Any,
    settings: Any,
    phone_settings: Any,
    to_number: str,
    agenda: str,
    event_bus: Any | None = None,
    origin_session_key: str | None = None,
    *,
    engine: str = "openai_realtime",
    realtime_meta: dict | None = None,
) -> dict:
    """Initiate an outbound phone call via Twilio.

    Shared by the HTTP endpoints, the voice outreach tools, and the subagent
    service. Returns {"call_id", "call_sid", "status"} on success or
    {"error": ...} on failure. Dispatch metadata is persisted to the
    phone_calls row (not just the in-memory cache) so the media-stream handler
    can recover it after a restart.
    """
    if not phone_settings.enabled:
        return {"error": "Phone subsystem is not enabled"}

    call_id = str(uuid4())
    session_key = f"agent:main:phone:call:{call_id}"
    meta = realtime_meta or {}
    subagent_id = meta.get("subagent_id")

    from twilio.rest import Client

    client = Client(
        phone_settings.twilio_account_sid,
        phone_settings.twilio_auth_token,
        # Empty region = default us1 (Ashburn). Set BOB_PHONE_TWILIO_REGION=au1
        # once an au1-hosted project+number exist to keep media in Sydney.
        **({"region": phone_settings.twilio_region} if phone_settings.twilio_region else {}),
    )
    base_url = phone_settings.base_url or settings.resolved_public_url

    # Prewarm the OpenAI Realtime session while the phone rings, so the
    # media stream attaches to a live, fully-configured session at answer
    # (the callee's greeting must not ride a setup backlog — see
    # services/realtime_prewarm.py). Best-effort: claim falls back to
    # connect-at-answer on any failure.
    if engine == "openai_realtime":
        try:
            from bob_server.services import realtime_prewarm
            realtime_prewarm.start_prewarm(
                call_id, db=db, settings=settings,
                phone_number=to_number, agenda=agenda, meta=meta,
            )
        except Exception:
            logger.warning("Prewarm start failed for call %s; continuing", call_id, exc_info=True)

    call = client.calls.create(
        to=to_number,
        from_=phone_settings.twilio_phone_number,
        twiml=build_stream_twiml(base_url),
        status_callback=f"{base_url}/phone/status",
        status_callback_event=["initiated", "ringing", "answered", "completed"],
    )
    sid = call.sid or ""
    if not sid:
        if engine == "openai_realtime":
            from bob_server.services import realtime_prewarm
            await realtime_prewarm.discard(call_id)
        return {"error": "Twilio returned no call SID"}

    call_agendas[sid] = {
        "agenda": agenda,
        "phone_number": to_number,
        "call_id": call_id,
        "session_key": session_key,
        "origin_session_key": origin_session_key,
        "direction": "outbound",
        "engine": engine,
        "realtime_meta": meta,
    }

    from bob_server.repositories.phone_calls import PhoneCallRepository
    await PhoneCallRepository(db).insert_outbound(
        call_id=call_id, call_sid=sid, phone_number=to_number, agenda=agenda,
        engine=engine, realtime_meta_json=json.dumps(meta),
        subagent_id=subagent_id, origin_session_key=origin_session_key)

    logger.info("Initiated call %s to %s (engine=%s)", sid, to_number, engine)

    if event_bus:
        await event_bus.publish("phone.call.ringing", {
            "call_id": call_id,
            "phone_number": to_number,
            "direction": "outbound",
            "agenda": agenda,
        })

    return {"call_sid": sid, "call_id": call_id, "status": call.status}


class VoiceDispatchService(BaseService):
    """Dispatch a goal-oriented voice call to a contact (phone or voice_link)."""

    async def dispatch_contact_call(
        self,
        subagent_id: str,
        task: str,
        contact_id: str | None,
        modality: str,
        parent_session_key: str,
    ) -> dict[str, Any]:
        """Resolve contact, build instructions, dispatch a voice_link or phone call.

        Raises on any failure (caller marks the subagent failed). Returns a dict
        with either ``voice_url`` (voice_link) or ``call_sid`` / ``call_id`` (phone).
        """
        if not contact_id:
            raise ValueError(
                "openai_voice subagent requires contact_id — look up the contact first "
                "with a contact search tool, then pass their id."
            )
        modality = normalise_voice_modality(modality) or "phone"
        if modality not in ("phone", "voice_link"):
            raise ValueError(f"unknown modality: {modality!r} — use 'phone' or 'voice_link'")

        from bob_server.repositories.contacts import ContactRepository
        contact = await ContactRepository(self.db).get(contact_id)
        if contact is None:
            raise ValueError(f"contact not found: {contact_id}")

        await self._update_subagent_status(subagent_id, "running")
        person_conversation_id = await self._bind_call_to_person(subagent_id, contact)

        # Occupancy (Phase VI item 6): register the live call BEFORE placing
        # it — enforces MAX_LIVE_CALLS and lets ingress queue inbound text on
        # this person's conversation for the post-call turn.
        if person_conversation_id:
            from bob_server.services import occupancy
            occupancy.mark_live(person_conversation_id, subagent_id)

        settings = self._get_settings()
        instructions = build_outbound_instructions(contact["name"], task)

        if modality == "voice_link":
            from bob_server.services.voice_session_service import VoiceSessionService
            # Ported from the retired reach_out_with_voice_call tool: the call
            # transcript/memory attaches to the contact's DM session (not the
            # requesting group), and the outcome reports back to whoever asked.
            origin = parent_session_key
            report_back: str | None = None
            digits = re.sub(r"\D", "", contact["phone_number"] or "")
            if digits:
                dm_session = f"agent:main:whatsapp:dm:{digits}"
                origin = dm_session
                if parent_session_key and parent_session_key != dm_session:
                    report_back = parent_session_key
            session = await VoiceSessionService(self.ctx).create(
                origin_session_key=origin,
                voice=settings.openai_realtime.voice,
                goal=task,
                report_back_session_key=report_back,
                subagent_id=subagent_id,
                phone_number=contact["phone_number"] or "",
            )
            return {
                "voice_url": session["url"],
                # The tool doesn't DM the link itself — the LLM does, with its
                # own intro. Nudge it so the two-step doesn't get dropped.
                "next_step": (
                    f"Send voice_url to {contact['name']} via WhatsApp now "
                    f"(e.g. an intro line plus the link); the call starts when "
                    f"they tap it."
                ),
            }

        # modality == "phone"
        phone_settings = settings.phone
        if not phone_settings.enabled:
            raise ValueError("Phone subsystem is not enabled")
        if not contact["phone_number"]:
            raise ValueError(f"contact {contact['name']} has no phone number")

        result = await initiate_outbound_call(
            db=self.db,
            settings=settings,
            phone_settings=phone_settings,
            to_number=contact["phone_number"],
            agenda=task,
            event_bus=self.ctx.event_bus,
            origin_session_key=parent_session_key,
            engine="openai_realtime",
            realtime_meta={
                "instructions": instructions,
                "voice": "",
                "subagent_id": subagent_id,
            },
        )
        if "error" in result:
            raise ValueError(result["error"])

        return {"call_id": result["call_id"], "call_sid": result["call_sid"]}

    async def _bind_call_to_person(self, subagent_id: str, contact: dict[str, Any]) -> str | None:
        """Voice as a binding (Phase VI item 5): the call's subagent session
        key becomes a binding on the person's conversation, so transcripts
        and outcomes land as events on the person, not on an orphan key.
        Returns the person's conversation id (None if unresolvable)."""
        digits = re.sub(r"\D", "", contact.get("phone_number") or "")
        if not digits:
            return None
        from bob_server.repositories.subagents import SubagentRepository
        call_session_key = await SubagentRepository(self.db).session_key_of(subagent_id)
        if call_session_key is None:
            return None
        try:
            from bob_server.repositories.conversations import ConversationRepository
            repo = ConversationRepository(self.db)
            conv = await repo.ensure(f"agent:main:whatsapp:dm:{digits}")
            await repo.bind(
                call_session_key, conv["id"],
                channel="voice", address=contact.get("phone_number"),
                endpoint_kind="call")
            return conv["id"]
        except Exception:
            logger.warning("failed to bind call %s to person conversation",
                           subagent_id[:8], exc_info=True)
            return None

    async def _update_subagent_status(self, subagent_id: str, status: str) -> None:
        from bob_server.repositories.subagents import SubagentRepository
        await SubagentRepository(self.db).set_status(
            subagent_id, status, utcnow().isoformat())
