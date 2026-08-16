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
    supply the goal: callee speaks first → brief greeting → short dialogue
    turns → plain spoken language → end-call protocol. Bob does not lead with
    "I'm an AI" or "I'm calling on Mike's behalf" (both cloud the opening) —
    whose behalf he's calling on is revealed only if the goal requires it or
    the person asks; AI status only if asked. This is the canonical builder —
    previously three call sites assembled their own (drifting) copies.
    """
    name_hint = f" The person you're calling is {contact_name}." if contact_name else ""
    preamble = (
        "You are Bob, an AI assistant placing a real voice call on behalf of Mike. "
        "Phone-call convention: when the person answers, do NOT start talking "
        "immediately — wait a moment and let THEM speak first (they will usually "
        "say 'hello'). Only after their greeting, say hello and introduce "
        "yourself briefly. Do NOT announce up front that you're an AI or a bot, "
        "and do NOT open with whose behalf you're calling on — both cloud the "
        "conversation. Reveal whose behalf you're calling on only when the goal "
        "below calls for it or the person asks. If the person asks directly "
        "whether you're an AI or who you're calling on behalf of, answer "
        "honestly and move on.{name_hint} "
        "CRITICAL: use ONLY the name given above for the person you're calling. "
        "Never invent, guess, or repeat a different name — connection noise is "
        "often mis-heard as a name. If the first thing you hear is silence, a "
        "brief noise, or something unintelligible, it was not a greeting: wait "
        "for a clear human voice. If you truly cannot make out what they said, "
        "say 'Sorry, I didn't quite catch that' and let them repeat — do not "
        "fabricate content for what you heard. "
        "Speak in plain conversational language — no emojis, no markdown, no lists, no URLs. "
        "Keep each of your turns SHORT — a sentence or two at most, never a "
        "monologue. Ask one question at a time, and after each thing you say, "
        "stop and listen. Do not recite your goal all at once; work through it "
        "one exchange at a time as a natural dialogue. "
        "If they ask to stop or want to end the call, respect that immediately. "
        "When the conversation has reached its natural close — you have what you need, "
        "or it's clear you won't get it — call the end_call tool. Do not announce that "
        "you're hanging up; just call the tool after your closing line."
    ).format(name_hint=name_hint)
    return f"{preamble}\n\n--- Your goal on this call ---\n{goal}".strip()


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
    try:
        await db.execute(
            """UPDATE subagents
               SET status = 'completed', result = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (result_text[:4000], subagent_id),
        )
    except Exception:
        logger.warning("Failed to mark voice subagent %s completed", subagent_id[:8], exc_info=True)


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
        await db.execute(
            "UPDATE phone_calls SET transcript = ? WHERE id = ?",
            (transcript, call_id),
        )
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
    row = await db.fetch_one(
        "SELECT id, phone_number, direction, agenda, engine, realtime_meta FROM phone_calls WHERE call_sid = ?",
        (call_sid,),
    )
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

    client = Client(phone_settings.twilio_account_sid, phone_settings.twilio_auth_token)
    base_url = phone_settings.base_url or settings.resolved_public_url

    call = client.calls.create(
        to=to_number,
        from_=phone_settings.twilio_phone_number,
        url=f"{base_url}/phone/twiml",
        status_callback=f"{base_url}/phone/status",
        status_callback_event=["initiated", "ringing", "answered", "completed"],
    )
    sid = call.sid or ""
    if not sid:
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

    await db.execute(
        """INSERT INTO phone_calls
           (id, call_sid, phone_number, direction, status, agenda, engine,
            realtime_meta, subagent_id, origin_session_key, started_at)
           VALUES (?, ?, ?, 'outbound', 'ringing', ?, ?, ?, ?, ?, datetime('now'))""",
        (call_id, sid, to_number, agenda, engine,
         json.dumps(meta), subagent_id, origin_session_key),
    )

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

        contact = await self.db.fetch_one(
            "SELECT id, name, phone_number FROM contacts WHERE id = ? AND deleted_at IS NULL",
            (contact_id,),
        )
        if contact is None:
            raise ValueError(f"contact not found: {contact_id}")

        await self._update_subagent_status(subagent_id, "running")

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

    async def _update_subagent_status(self, subagent_id: str, status: str) -> None:
        await self.db.execute(
            "UPDATE subagents SET status = ?, updated_at = ? WHERE id = ?",
            (status, utcnow().isoformat(), subagent_id),
        )
