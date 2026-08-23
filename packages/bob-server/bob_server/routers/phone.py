"""Phone call integration via Twilio Media Streams.

HTTP webhooks for Twilio call control and the WebSocket endpoint for
bidirectional audio streaming through the OpenAI Realtime bridge. Call
placement and dispatch metadata live in
services/voice_dispatch_service.py; this router owns the Twilio-facing
HTTP/WS surface only.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from typing import Any
from uuid import uuid4

from bob_server.services.base import utcnow
from bob_server.services.voice_dispatch_service import (
    build_inbound_instructions,
    call_agendas,
    extract_outcome,
    hangup_twilio_call,
    initiate_outbound_call,
    load_call_meta,
    mark_voice_subagent_complete,
    persist_call_transcript,
)

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["phone"])


def _normalize_phone(raw: str) -> str:
    """Normalize a phone number to +CC format."""
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+"):
        return "+" + digits
    if digits.startswith("0"):
        return "+61" + digits[1:]
    if digits.startswith("61"):
        return "+" + digits
    return "+" + digits


async def _setup_inbound_call(db: Any, settings: Any, call_sid: str, from_number: str) -> None:
    """Set up session data for an inbound call: contact resolution, agenda, DB record."""
    if call_sid in call_agendas:
        return

    call_id = str(uuid4())
    session_key = f"agent:main:phone:call:{call_id}"
    phone_number = _normalize_phone(from_number)

    # Resolve contact
    contact_id: str | None = None
    is_trusted = False
    contact_name: str | None = None
    from bob_server.repositories.contacts import ContactRepository
    contacts_repo = ContactRepository(db)
    contact = await contacts_repo.get_by_phone(phone_number)
    if contact:
        contact_id = contact["id"]
        is_trusted = bool(contact.get("is_trusted", 0))
        contact_name = contact.get("name")
    else:
        # Auto-seed an untrusted contact
        contact_id = await contacts_repo.create(
            name=phone_number, phone_number=phone_number, is_trusted=0)

    # Resolve agenda
    from bob_server.context import AppContext
    from bob_server.services.session_agenda_service import SessionAgendaService
    ctx = AppContext(db=db, settings=settings)
    agenda_svc = SessionAgendaService(ctx)
    agenda = await agenda_svc.get_effective_agenda(
        session_key, "phone", contact_id=contact_id, is_trusted=is_trusted,
    )

    instructions = build_inbound_instructions(phone_number, contact_name, agenda)

    # Register the endpoint binding
    from bob_server.repositories.conversations import ConversationRepository
    await ConversationRepository(ctx.db).register_endpoint(
        session_key, endpoint_kind="call", contact_id=str(contact_id))

    # Insert DB record
    from bob_server.repositories.phone_calls import PhoneCallRepository
    await PhoneCallRepository(db).insert_inbound(
        call_id=call_id, call_sid=call_sid, phone_number=phone_number, agenda=agenda)

    # Store for the media_stream handler — inbound calls are realtime too.
    call_agendas[call_sid] = {
        "agenda": agenda or "",
        "phone_number": phone_number,
        "call_id": call_id,
        "session_key": session_key,
        "contact_id": contact_id,
        "direction": "inbound",
        "engine": "openai_realtime",
        "realtime_meta": {"instructions": instructions, "voice": ""},
    }

    logger.info("Set up inbound call %s from %s (contact=%s, trusted=%s)",
                call_sid, phone_number, contact_id, is_trusted)


@router.post("/call")
async def initiate_call(request: Request) -> dict:
    """Initiate an outbound phone call via Twilio (Realtime engine)."""
    body = await request.json()
    to_number = body.get("to", "").strip()
    if not to_number:
        return {"error": "Missing 'to' phone number"}

    agenda = body.get("agenda", "").strip()
    from bob_server.services.voice_dispatch_service import build_outbound_instructions
    instructions = build_outbound_instructions(goal=agenda)

    return await initiate_outbound_call(
        db=request.app.state.db,
        settings=request.app.state.settings,
        phone_settings=request.app.state.settings.phone,
        to_number=to_number,
        agenda=agenda,
        event_bus=request.app.state.event_bus,
        engine="openai_realtime",
        realtime_meta={"instructions": instructions, "voice": ""},
    )


@router.post("/twiml")
async def twiml_webhook(request: Request) -> PlainTextResponse:
    """Return TwiML that connects the call to our Media Stream WebSocket.

    For inbound calls, also sets up session data, contact resolution, and DB record.
    """
    db = request.app.state.db
    settings = request.app.state.settings

    # Twilio sends form data with call parameters
    form = await request.form()
    call_sid = str(form.get("CallSid", ""))
    direction = str(form.get("Direction", ""))
    from_number = str(form.get("From", ""))

    if direction == "inbound" and call_sid:
        await _setup_inbound_call(db, settings, call_sid, from_number)

    base_url = settings.phone.base_url or settings.resolved_public_url

    from bob_server.services.voice_dispatch_service import build_stream_twiml
    twiml = build_stream_twiml(base_url)

    logger.info("TwiML webhook: returning stream TwiML for %s/phone/media", base_url)
    return PlainTextResponse(twiml, media_type="application/xml")


async def _maybe_dispatch_call_result(
    db: Any,
    settings: Any,
    app_state: Any,
    call_sid: str,
    call_status: str,
    call_id_override: str | None = None,
) -> None:
    """Dispatch call result to originating session if applicable.

    Double-dispatch is guarded atomically by claiming the row: the UPDATE only
    fires when result_dispatched_at is NULL, so it is safe across restarts and
    concurrent callers (previously an in-memory set).
    """
    # Look up the call record to get origin_session_key and call_id
    from bob_server.repositories.phone_calls import PhoneCallRepository
    calls_repo = PhoneCallRepository(db)
    if call_sid:
        call_row = await calls_repo.get_by_sid(call_sid)
    elif call_id_override:
        call_row = await calls_repo.get(call_id_override)
    else:
        return

    if not call_row or not call_row["origin_session_key"]:
        return

    call_id = call_row["id"]
    if not await calls_repo.claim_result_dispatch(call_id):
        return

    origin_session_key = call_row["origin_session_key"]
    agenda = call_row["agenda"] or ""

    from bob_server.context import AppContext
    from bob_server.services.phone_call_result_service import dispatch_call_result

    ctx = AppContext(db=db, settings=settings)
    ctx.whatsapp_bridge = getattr(app_state, "whatsapp_bridge_service", None)
    wa_service = ctx.whatsapp_bridge

    asyncio.create_task(dispatch_call_result(
        ctx,
        call_id=call_id,
        origin_session_key=origin_session_key,
        agenda=agenda,
        status=call_status,
        wa_service=wa_service,
    ))

    logger.info(
        "Dispatching call result for %s (status=%s) to origin session %s",
        call_id, call_status, origin_session_key,
    )


async def _append_call_status_event(db: Any, call_sid: str, call_status: str, call_duration: str) -> None:
    """Append a call.status event (Bob3 Phase I ingress, audit-only).

    Twilio retries webhooks, so external_id = sid:status gives accept-once.
    Best-effort: an append failure must never break the webhook.
    """
    if not call_sid or not call_status:
        return
    try:
        from bob_server.repositories import Event, EventLogRepository

        from bob_server.repositories.phone_calls import PhoneCallRepository
        row = await PhoneCallRepository(db).get_by_sid(call_sid)
        phone = (row or {}).get("phone_number") or "unknown"
        binding = f"agent:main:phone:dm:{phone.lstrip('+')}"
        await EventLogRepository(db).append(Event(
            event_type="call.status",
            binding_key=binding,
            conversation_id=binding,
            source="phone",
            external_id=f"{call_sid}:{call_status}",
            payload={
                "call_sid": call_sid,
                "status": call_status,
                "duration": call_duration or None,
                "direction": (row or {}).get("direction"),
            },
        ))
    except Exception:
        logger.warning("call.status event append failed for %s", call_sid, exc_info=True)


@router.post("/status")
async def call_status(request: Request) -> dict:
    """Handle call status callbacks from Twilio."""
    form = await request.form()
    call_sid = form.get("CallSid", "")
    call_status = form.get("CallStatus", "")
    call_duration = form.get("CallDuration", "")
    logger.info("Call %s status: %s (duration=%s)", call_sid, call_status, call_duration)

    db = request.app.state.db
    await _append_call_status_event(db, call_sid, call_status, call_duration)

    # Persist status to DB
    from bob_server.repositories.phone_calls import PhoneCallRepository
    calls_repo = PhoneCallRepository(db)
    if call_status in ("completed", "failed", "busy", "no-answer", "canceled"):
        await calls_repo.complete_by_sid(
            call_sid, call_status,
            duration_seconds=int(call_duration) if call_duration else None)
        # Clean up cached call data
        call_agendas.pop(call_sid, None)

        # And any prewarmed session the media stream never claimed
        # (no-answer/busy/failed — the stream never arrives).
        row = await calls_repo.get_by_sid(call_sid)
        if row is not None:
            from bob_server.services import realtime_prewarm
            await realtime_prewarm.discard(row["id"])

        # Dispatch call result to originating session if applicable
        await _maybe_dispatch_call_result(
            db=db,
            settings=request.app.state.settings,
            app_state=request.app.state,
            call_sid=call_sid,
            call_status=call_status,
        )
    elif call_status == "ringing":
        await calls_repo.set_status_by_sid(call_sid, "ringing")
    elif call_status == "in-progress":
        await calls_repo.set_status_by_sid(call_sid, "active")

    return {"ok": True}


@router.get("/calls")
async def list_calls(request: Request) -> dict:
    """List recent phone calls."""
    db = request.app.state.db
    from bob_server.repositories.phone_calls import PhoneCallRepository
    calls = await PhoneCallRepository(db).recent(limit=50)
    return {"calls": [dict(c) for c in calls]}


@router.get("/calls/{call_id}")
async def get_call(call_id: str, request: Request) -> dict:
    """Get a single call's status and exchanges."""
    db = request.app.state.db

    # Support lookup by call_sid or internal id
    from bob_server.repositories.phone_calls import PhoneCallRepository
    call = await PhoneCallRepository(db).get(call_id)
    if not call:
        return {"error": "Call not found"}

    return {"call": call, "exchanges": []}


@router.post("/calls/{call_id}/hangup")
async def hangup_call(call_id: str, request: Request) -> dict:
    """Hang up an active or ringing phone call via Twilio."""
    db = request.app.state.db
    from bob_server.repositories.phone_calls import PhoneCallRepository
    call = await PhoneCallRepository(db).get(call_id)
    if not call:
        return {"error": "Call not found"}
    if call["status"] not in ("active", "ringing"):
        return {"error": f"Call is {call['status']}, cannot hang up"}

    if hangup_twilio_call(request.app.state.settings, call["call_sid"]):
        return {"ok": True}
    return {"error": "Failed to hang up call via Twilio"}


@router.websocket("/media")
async def media_stream(websocket: WebSocket) -> None:
    """Handle Twilio Media Stream WebSocket — routes to the Realtime bridge.

    All calls (inbound and outbound) run the OpenAI Realtime engine. Dispatch
    metadata comes from the in-memory cache, falling back to the phone_calls
    row so a server restart between dial and answer doesn't kill the call.
    """
    await websocket.accept()
    # The funnel preserves the origin client (proxy protocol / X-Forwarded-For),
    # so this shows WHERE Twilio's media server connects from — needed to
    # reason about stream-establishment latency (2026-08-18: control-plane
    # webhooks originated in Ashburn despite both legs being AU numbers).
    logger.info(
        "Twilio Media Stream connected (peer=%s xff=%s)",
        websocket.client, websocket.headers.get("x-forwarded-for", "-"),
    )

    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break

            if "text" not in msg:
                continue

            try:
                data = json.loads(msg["text"])
            except (json.JSONDecodeError, TypeError):
                continue

            event = data.get("event")

            if event == "connected":
                logger.info("Twilio Media Stream session connected")

            elif event == "start":
                start_data = data.get("start", {})
                stream_sid = data.get("streamSid", start_data.get("streamSid", ""))
                call_sid = start_data.get("callSid", "")
                stored = call_agendas.get(call_sid) or await load_call_meta(websocket.app.state.db, call_sid)
                if isinstance(stored, dict) and stored.get("engine") == "openai_realtime":
                    await _run_realtime_call(websocket, call_sid, stream_sid, stored)
                    return
                logger.warning(
                    "Twilio stream %s has no realtime call record (call_sid=%s); closing",
                    stream_sid, call_sid,
                )
                try:
                    await websocket.close()
                except Exception:
                    pass
                return

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.error("Error in media stream handler", exc_info=True)

    logger.info("Twilio Media Stream disconnected")


async def _run_realtime_call(websocket: WebSocket, call_sid: str, stream_sid: str, stored: dict) -> None:
    """Handle a Twilio Media Stream using the OpenAI Realtime bridge.

    Reads Twilio media frames (μ-law), feeds them to a TwilioMediaSource, and
    runs the RealtimeBridge concurrently. On stop/disconnect the bridge winds
    down and the transcript/recording are persisted.
    """
    from bob_server.context import AppContext
    from bob_server.services.realtime_bridge import RealtimeBridge, TwilioMediaSource
    from bob_server.services.realtime_tools import make_realtime_tools

    app_state = websocket.app.state
    settings_full = app_state.settings
    rt_settings = settings_full.openai_realtime
    db = app_state.db
    event_bus = getattr(app_state, "event_bus", None)

    call_id = stored.get("call_id") or str(uuid4())
    phone_number = stored.get("phone_number", "")
    meta = stored.get("realtime_meta") or {}
    instructions = meta.get("instructions") or stored.get("agenda", "")
    voice = meta.get("voice") or rt_settings.voice
    max_duration = min(
        float(meta.get("max_duration") or rt_settings.max_call_duration_seconds),
        rt_settings.max_call_duration_seconds,
    )

    ctx = AppContext(db=db, settings=settings_full)
    tools = make_realtime_tools(ctx, phone_number=phone_number)

    # Phone-call convention: on an outbound call the callee speaks first
    # ("hello"); the agent replies to their greeting. Inbound callers have
    # already spoken by connect time, so the agent opens there.
    speak_first = (stored.get("direction") or "outbound") != "outbound"

    # Persist partial transcript after every turn so phone_calls.transcript is
    # readable mid-call and survives a bridge cleanup hang.
    async def on_turn(transcript: str) -> None:
        await persist_call_transcript(db, call_id, transcript)

    source = TwilioMediaSource(websocket, stream_sid)

    # Claim the session prewarmed at placement (see services/realtime_prewarm.py)
    # so the greeting flows into a live, fully-configured session at 1×. Falls
    # back to connecting here — the pre-prewarm behaviour — on any failure.
    from bob_server.services import realtime_prewarm
    prewarmed = await realtime_prewarm.claim(call_id)
    if prewarmed is not None:
        bridge = prewarmed
        bridge.audio = source
    else:
        bridge = RealtimeBridge(
            source,
            api_key=settings_full.openai.api_key,
            model=rt_settings.model,
            instructions=instructions,
            voice=voice,
            tools=tools,
            max_duration_seconds=max_duration,
            turn_detection=rt_settings.turn_detection,
            on_turn=on_turn,
            speak_first=speak_first,
        )

    from bob_server.repositories.phone_calls import PhoneCallRepository
    await PhoneCallRepository(db).attach_stream(call_id, stream_sid)
    if event_bus:
        await event_bus.publish("phone.call.active", {"call_id": call_id})

    await source.start()
    bridge_task = asyncio.create_task(bridge.run())

    async def _hangup_when_bridge_ends() -> None:
        """end_call / timeout / error end the OpenAI session but NOT the phone
        leg — and this handler's read loop only exits when Twilio closes the
        stream, so without an explicit hang-up the call sits connected in dead
        air, billing minutes, and the finalize path never runs (observed
        2026-08-14: 3.5 min of silence after the agent ended a voicemail call).
        Hanging up makes Twilio close the stream, which unblocks the read loop.
        """
        result = await bridge_task
        if result.end_reason in ("end_tool", "timeout", "error") and not source.input_closed():
            logger.info("Bridge ended (%s) — hanging up Twilio call %s", result.end_reason, call_sid)
            hangup_twilio_call(settings_full, call_sid)

    hangup_task = asyncio.create_task(_hangup_when_bridge_ends())

    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if "text" not in msg:
                continue
            try:
                data = json.loads(msg["text"])
            except (json.JSONDecodeError, TypeError):
                continue
            event = data.get("event")
            if event == "media":
                payload = data.get("media", {}).get("payload", "")
                if payload:
                    source.feed_inbound_mulaw(base64.b64decode(payload))
            elif event == "stop":
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.error("Error in realtime media loop", exc_info=True)
    finally:
        if not hangup_task.done():
            hangup_task.cancel()
            try:
                await hangup_task
            except (asyncio.CancelledError, Exception):
                pass
        source.signal_closed()
        try:
            result = await asyncio.wait_for(bridge_task, timeout=10)
        except (asyncio.TimeoutError, Exception):
            result = None
        await source.aclose()

        transcript = result.transcript if result else ""
        duration = result.duration_seconds if result else 0.0

        # Structured outcome from report_success / report_failure tool calls.
        outcome = extract_outcome(result.tool_calls) if result else None
        if outcome is None and result and result.tool_calls:
            # No outcome tool reported — keep any other tool results visible in
            # the stored transcript as a fallback.
            lines = ["\n\n--- Tool outcomes ---"]
            for tc in result.tool_calls:
                args_str = json.dumps(tc.get("arguments", {}))
                lines.append(f"{tc.get('name')}: {args_str} -> {tc.get('output')}")
            transcript = (transcript + "\n".join(lines)).strip()

        rec_path = None
        try:
            calls_dir = settings_full.config_dir / "harness" / "calls"
            rec = source.finalize_recording(calls_dir, call_id)
            if rec:
                rec_path = str(calls_dir / rec[0])
        except Exception:
            logger.warning("Failed to finalize realtime recording", exc_info=True)

        try:
            from bob_server.repositories.phone_calls import PhoneCallRepository
            await PhoneCallRepository(db).finalize(
                call_id, transcript=transcript, recording_path=rec_path,
                duration_seconds=duration,
                outcome_json=json.dumps(outcome) if outcome else None)
            if event_bus:
                await event_bus.publish("phone.call.completed", {"call_id": call_id})
        except Exception:
            logger.warning("Failed to finalize realtime call record", exc_info=True)

        # Mark the linked subagent completed so the parent sees a clean lifecycle.
        subagent_id = meta.get("subagent_id")
        if subagent_id:
            await mark_voice_subagent_complete(db, subagent_id, transcript)

        await _maybe_dispatch_call_result(
            db=db,
            settings=settings_full,
            app_state=app_state,
            call_sid=call_sid,
            call_status="completed",
            call_id_override=call_id,
        )
        logger.info(
            "Realtime call %s ended (reason=%s, %.1fs)",
            call_id, result.end_reason if result else "?", duration,
        )
