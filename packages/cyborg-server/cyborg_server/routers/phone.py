"""Phone call integration via Twilio Media Streams.

Provides HTTP webhooks for Twilio call control and a WebSocket endpoint
for bidirectional audio streaming through the voice pipeline.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from cyborg_server.services.base import utcnow

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["phone"])

# Shared in-memory store for call agenda data, used by both the HTTP endpoint
# and the LLM phone tool. Previously lived on app.state.
_call_agendas: dict[str, dict] = {}


def _build_phone_context() -> str:
    return (
        "This is a live phone call to a real person on their mobile phone. "
        "If the person says a phone greeting like 'Hi', 'Hello', or 'X speaking', "
        "introduce yourself by your name, explain that you are an AI assistant, "
        "and say who you are calling on behalf of (the caller's name is in the agenda). "
        "Respond in plain spoken language: no emojis, no markdown, no formatting. "
        "To end the call at any time (e.g., the other person says goodbye, "
        "or the conversation has reached its natural conclusion), include <hangup/> "
        "at the end of your response. The call will be terminated after your response "
        "is fully spoken. Do not mention the hangup token to the caller."
    )


class _CallRecorderProxy:
    """Wraps a VoiceTransport and captures transcript/latency for call logging.

    Saves the exchange to DB when the latency message arrives (the last
    message VoiceService sends), ensuring all data is captured after the
    full pipeline completes.
    """

    def __init__(self, transport: Any, call_id: str, exchange_index: int,
                 db: Any, utterance_time: float = 0.0) -> None:
        self._transport = transport
        self._call_id = call_id
        self._exchange_index = exchange_index
        self._db = db
        self._utterance_time = utterance_time
        self._saved = False
        self.user_transcript: str = ""
        self.assistant_transcript: str = ""
        self.latency: dict = {}
        self.done: asyncio.Event = asyncio.Event()
        self.hangup_requested: bool = False

    async def send_audio(self, wav_bytes: bytes) -> None:
        await self._transport.send_audio(wav_bytes)

    async def send_status(self, state: str) -> None:
        await self._transport.send_status(state)

    async def send_error(self, message: str) -> None:
        await self._transport.send_error(message)
        self.done.set()

    async def send_message(self, msg_type: str, data: dict) -> None:
        if msg_type == "transcript":
            self.user_transcript = data.get("text", "")
        elif msg_type == "response_text":
            self.assistant_transcript = data.get("text", "")
        elif msg_type == "latency":
            self.latency = data
            await self._save_exchange()
            self.done.set()
        elif msg_type == "hangup":
            self.hangup_requested = True
            self.done.set()
        await self._transport.send_message(msg_type, data)

    async def _save_exchange(self) -> None:
        if self._saved:
            return
        self._saved = True
        if not self.user_transcript and not self.assistant_transcript:
            return
        from datetime import datetime, timezone
        started_iso = (
            datetime.fromtimestamp(self._utterance_time, tz=timezone.utc).isoformat()
            if self._utterance_time else None
        )
        try:
            await self._db.execute(
                """INSERT INTO phone_call_exchanges
                   (call_id, exchange_index, user_transcript, assistant_transcript,
                    stt_ms, llm_total_ms, tts_first_chunk_ms, e2e_ms,
                    llm_prepare_ms, llm_stream_ms, tts_wait_lock_ms, tts_generate_ms,
                    started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (self._call_id, self._exchange_index,
                 self.user_transcript, self.assistant_transcript,
                 self.latency.get("stt_ms"),
                 self.latency.get("llm_total_ms"),
                 self.latency.get("tts_first_chunk_ms"),
                 self.latency.get("e2e_ms"),
                 self.latency.get("llm_prepare_ms"),
                 self.latency.get("llm_stream_ms"),
                 self.latency.get("tts_wait_lock_ms"),
                 self.latency.get("tts_generate_ms"),
                 started_iso),
            )
        except Exception:
            logger.warning("Failed to log phone call exchange", exc_info=True)


async def _emit_event(app_state: Any, event_type: str, payload: dict) -> None:
    event_bus = getattr(app_state, "event_bus", None)
    if event_bus:
        await event_bus.publish(event_type, payload)


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


def _build_inbound_phone_context(phone_number: str, contact_name: str | None = None) -> str:
    name_hint = f" The caller's name is {contact_name}." if contact_name else ""
    return (
        "This is an inbound phone call from a real person. "
        "They called your number, so greet them and find out how you can help. "
        f"Their phone number is {phone_number}.{name_hint} "
        "Respond in plain spoken language: no emojis, no markdown, no formatting. "
        "To end the call at any time, include <hangup/> at the end of your response. "
        "Do not mention the hangup token to the caller."
    )


async def _setup_inbound_call(db: Any, settings: Any, call_sid: str, from_number: str) -> None:
    """Set up session data for an inbound call: contact resolution, agenda, DB record."""
    if call_sid in _call_agendas:
        return

    call_id = str(uuid4())
    session_key = f"agent:main:phone:call:{call_id}"
    phone_number = _normalize_phone(from_number)

    # Resolve contact
    contact_id: str | None = None
    is_trusted = False
    contact_name: str | None = None
    contact = await db.fetch_one(
        "SELECT id, is_trusted, name FROM contacts WHERE phone_number = ? AND deleted_at IS NULL LIMIT 1",
        (phone_number,),
    )
    if contact:
        contact_id = contact["id"]
        is_trusted = bool(contact.get("is_trusted", 0))
        contact_name = contact.get("name")
    else:
        # Auto-seed an untrusted contact
        from uuid import uuid4 as _uuid4
        new_id = str(_uuid4())
        now_iso = utcnow().isoformat()
        await db.execute(
            """INSERT INTO contacts (id, name, phone_number, is_trusted, created_at, updated_at)
               VALUES (?, ?, ?, 0, ?, ?)""",
            (new_id, phone_number, phone_number, now_iso, now_iso),
        )
        contact_id = new_id

    # Resolve agenda
    from cyborg_server.context import AppContext
    from cyborg_server.services.session_agenda_service import SessionAgendaService
    ctx = AppContext(db=db, settings=settings)
    agenda_svc = SessionAgendaService(ctx)
    agenda = await agenda_svc.get_effective_agenda(
        session_key, "phone", contact_id=contact_id, is_trusted=is_trusted,
    )

    inbound_context = _build_inbound_phone_context(phone_number, contact_name)

    # Create session route
    from cyborg_server.services.session_route_service import SessionRouteService
    from cyborg_server.models import SessionRouteCreate, SessionRouteKind
    route_service = SessionRouteService(ctx)
    from cyborg_server.exceptions import ConflictError
    try:
        await route_service.create_route(SessionRouteCreate(
            channel="phone",
            session_key=session_key,
            kind=SessionRouteKind.DM,
            contact_id=contact_id,
        ))
    except ConflictError:
        pass

    # Insert DB record
    await db.execute(
        """INSERT INTO phone_calls (id, call_sid, phone_number, direction, status, agenda, started_at)
           VALUES (?, ?, ?, 'inbound', 'ringing', ?, datetime('now'))""",
        (call_id, call_sid, phone_number, agenda),
    )

    # Store for the media_stream handler
    _call_agendas[call_sid] = {
        "agenda": f"{inbound_context} {agenda}".strip() if agenda else inbound_context,
        "phone_number": phone_number,
        "call_id": call_id,
        "session_key": session_key,
        "contact_id": contact_id,
    }

    logger.info("Set up inbound call %s from %s (contact=%s, trusted=%s)",
                call_sid, phone_number, contact_id, is_trusted)


async def initiate_outbound_call(
    db: Any,
    settings: Any,
    phone_settings: Any,
    to_number: str,
    agenda: str,
    app_state: Any | None = None,
) -> dict:
    """Initiate an outbound phone call via Twilio.

    Shared by the HTTP endpoint and the LLM phone tool.
    Returns {"call_id", "call_sid", "status"} on success or {"error": ...} on failure.
    """
    if not phone_settings.enabled:
        return {"error": "Phone subsystem is not enabled"}

    call_id = str(uuid4())
    session_key = f"agent:main:phone:call:{call_id}"

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

    _call_agendas[call.sid] = {
        "agenda": agenda,
        "phone_number": to_number,
        "call_id": call_id,
        "session_key": session_key,
    }

    await db.execute(
        """INSERT INTO phone_calls (id, call_sid, phone_number, direction, status, agenda, started_at)
           VALUES (?, ?, ?, 'outbound', 'ringing', ?, datetime('now'))""",
        (call_id, call.sid, to_number, agenda),
    )

    logger.info("Initiated call %s to %s", call.sid, to_number)

    if app_state:
        await _emit_event(app_state, "phone.call.ringing", {
            "call_id": call_id,
            "phone_number": to_number,
            "direction": "outbound",
            "agenda": agenda,
        })

    return {"call_sid": call.sid, "call_id": call_id, "status": call.status}


@router.post("/call")
async def initiate_call(request: Request) -> dict:
    """Initiate an outbound phone call via Twilio."""
    body = await request.json()
    to_number = body.get("to", "").strip()
    if not to_number:
        return {"error": "Missing 'to' phone number"}

    agenda = body.get("agenda", "").strip()

    return await initiate_outbound_call(
        db=request.app.state.db,
        settings=request.app.state.settings,
        phone_settings=request.app.state.settings.phone,
        to_number=to_number,
        agenda=agenda,
        app_state=request.app.state,
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
    ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://")

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{ws_url}/phone/media" />
  </Connect>
</Response>"""

    logger.info("TwiML webhook: returning stream URL %s/phone/media", ws_url)
    return PlainTextResponse(twiml, media_type="application/xml")


@router.post("/status")
async def call_status(request: Request) -> dict:
    """Handle call status callbacks from Twilio."""
    form = await request.form()
    call_sid = form.get("CallSid", "")
    call_status = form.get("CallStatus", "")
    call_duration = form.get("CallDuration", "")
    logger.info("Call %s status: %s (duration=%s)", call_sid, call_status, call_duration)

    db = request.app.state.db

    # Persist status to DB
    if call_status in ("completed", "failed", "busy", "no-answer", "canceled"):
        if call_duration:
            await db.execute(
                """UPDATE phone_calls
                   SET status = ?, completed_at = datetime('now'), duration_seconds = ?
                   WHERE call_sid = ?""",
                (call_status, int(call_duration), call_sid),
            )
        else:
            await db.execute(
                """UPDATE phone_calls
                   SET status = ?, completed_at = datetime('now')
                   WHERE call_sid = ?""",
                (call_status, call_sid),
            )
        # Clean up stored call data
        call_data = _call_agendas.pop(call_sid, None)
        if call_data and isinstance(call_data, dict):
            # Finalize recording if transport captured audio
            transport = call_data.get("_transport")
            if transport and hasattr(transport, "finalize_recording"):
                settings = request.app.state.settings
                calls_dir = Path(settings.config_dir) / "harness" / "calls"
                result = transport.finalize_recording(calls_dir, call_data.get("call_id", call_sid))
                if result:
                    rel_path, _ = result
                    await db.execute(
                        "UPDATE phone_calls SET recording_path = ? WHERE call_sid = ?",
                        (str(calls_dir / rel_path), call_sid),
                    )
    elif call_status == "ringing":
        await db.execute(
            "UPDATE phone_calls SET status = 'ringing' WHERE call_sid = ?",
            (call_sid,),
        )
    elif call_status == "in-progress":
        await db.execute(
            "UPDATE phone_calls SET status = 'active' WHERE call_sid = ?",
            (call_sid,),
        )

    return {"ok": True}


@router.get("/calls")
async def list_calls(request: Request) -> dict:
    """List recent phone calls."""
    db = request.app.state.db
    calls = await db.fetch_all(
        """SELECT id, call_sid, phone_number, direction, status, agenda,
                  exchange_count, duration_seconds, recording_path,
                  started_at, completed_at
           FROM phone_calls
           ORDER BY started_at DESC
           LIMIT 50""",
    )
    return {"calls": [dict(c) for c in calls]}


@router.get("/calls/{call_id}")
async def get_call(call_id: str, request: Request) -> dict:
    """Get a single call's status and exchanges."""
    db = request.app.state.db

    # Support lookup by call_sid or internal id
    call = await db.fetch_one(
        "SELECT * FROM phone_calls WHERE id = ? OR call_sid = ?",
        (call_id, call_id),
    )
    if not call:
        return {"error": "Call not found"}

    exchanges = await db.fetch_all(
        """SELECT exchange_index, user_transcript, assistant_transcript,
                  stt_ms, llm_total_ms, tts_first_chunk_ms, e2e_ms,
                  started_at, created_at
           FROM phone_call_exchanges
           WHERE call_id = ?
           ORDER BY exchange_index""",
        (call["id"],),
    )
    return {"call": dict(call), "exchanges": [dict(e) for e in exchanges]}


@router.post("/calls/{call_id}/hangup")
async def hangup_call(call_id: str, request: Request) -> dict:
    """Hang up an active or ringing phone call via Twilio."""
    db = request.app.state.db
    call = await db.fetch_one(
        "SELECT call_sid, status FROM phone_calls WHERE id = ? OR call_sid = ?",
        (call_id, call_id),
    )
    if not call:
        return {"error": "Call not found"}
    if call["status"] not in ("active", "ringing"):
        return {"error": f"Call is {call['status']}, cannot hang up"}

    phone_settings = request.app.state.settings.phone
    from twilio.rest import Client
    client = Client(phone_settings.twilio_account_sid, phone_settings.twilio_auth_token)
    client.calls(call["call_sid"]).update(status="completed")

    return {"ok": True}


@router.websocket("/media")
async def media_stream(websocket: WebSocket) -> None:
    """Handle Twilio Media Stream WebSocket for bidirectional audio."""
    await websocket.accept()
    logger.info("Twilio Media Stream connected")

    engines = getattr(websocket.app.state, "voice_engines", None)
    if engines is None:
        logger.error("Voice engines not loaded — cannot handle phone call")
        await websocket.close()
        return

    settings = websocket.app.state.settings.phone
    db = websocket.app.state.db

    from cyborg_server.context import AppContext
    from cyborg_server.services.voice_transport import TwilioTransport

    ctx = AppContext(db=db, settings=websocket.app.state.settings, voice_engines=engines)

    transport: TwilioTransport | None = None
    agenda: str = ""
    call_id: str = ""
    exchange_index: int = 0
    processing_lock = asyncio.Lock()
    pipeline_task: asyncio.Task | None = None
    hello_task: asyncio.Task | None = None
    first_utterance_done = False
    media_count = 0

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
                transport = TwilioTransport(
                    websocket=websocket,
                    stream_sid=stream_sid,
                    silence_threshold=settings.silence_threshold,
                    silence_duration=settings.silence_duration,
                    record=settings.call_recording_enabled,
                )
                stored = _call_agendas.get(call_sid, {})
                if isinstance(stored, dict):
                    raw_agenda = stored.get("agenda", "")
                    call_id = stored.get("call_id", str(uuid4()))
                else:
                    raw_agenda = stored or ""
                    call_id = str(uuid4())
                phone_context = _build_phone_context()
                agenda = f"{phone_context} {raw_agenda}".strip() if raw_agenda else phone_context
                logger.info("Twilio stream started: %s (call: %s)", stream_sid, call_sid)

                # Update call record to active
                await db.execute(
                    """UPDATE phone_calls
                       SET stream_sid = ?, status = 'active'
                       WHERE id = ?""",
                    (stream_sid, call_id),
                )
                await _emit_event(websocket.app.state, "phone.call.active", {
                    "call_id": call_id,
                })

                # If no speech after 5s, say "Hello?"
                hello_task = asyncio.create_task(
                    _say_hello_if_silent(
                        websocket, transport, engines,
                        lambda: first_utterance_done,
                        lambda: transport._has_speech,
                        call_id, exchange_index, db,
                    )
                )

            elif event == "media":
                if transport is None:
                    continue

                payload = data.get("media", {}).get("payload", "")
                if not payload:
                    continue

                mulaw_bytes = base64.b64decode(payload)
                media_count += 1

                # Always feed audio for silence/speech detection
                transport.feed_inbound_audio(mulaw_bytes)

                if processing_lock.locked():
                    # Pipeline running — check for barge-in
                    if transport._has_speech and transport.is_speaking:
                        logger.info("Barge-in detected at media chunk %d", media_count)
                        transport.interrupt()
                        if pipeline_task and not pipeline_task.done():
                            pipeline_task.cancel()
                        # Reset transport state for new utterance detection
                        transport.clear_buffer()
                        transport.reset_interrupt()
                else:
                    # Pipeline idle — normal utterance detection
                    if transport.is_utterance_complete():
                        logger.info("Utterance complete after %d media chunks", media_count)
                        first_utterance_done = True
                        if hello_task and not hello_task.done():
                            hello_task.cancel()

                        # Play acknowledgment tick so the caller knows input was captured
                        from cyborg_server.services.voice_engines import generate_tone_wav
                        tick_wav = generate_tone_wav(frequency=1200, duration=0.06, sample_rate=16000, amplitude=0.2)
                        await transport.send_audio(tick_wav)

                        pcm16 = transport.get_accumulated_pcm16()
                        if len(pcm16) == 0:
                            continue

                        # Convert PCM16 to WAV bytes for the voice pipeline
                        import io
                        import soundfile as sf

                        buf = io.BytesIO()
                        sf.write(buf, pcm16, 16000, subtype="PCM_16", format="WAV")
                        wav_bytes = buf.getvalue()

                        utterance_time = time.time()

                        pipeline_task = asyncio.create_task(
                            _run_voice_pipeline(
                                websocket, transport, wav_bytes,
                                processing_lock, agenda, call_id, exchange_index, db, ctx,
                                utterance_time,
                            )
                        )
                        exchange_index += 1

            elif event == "stop":
                logger.info("Twilio stream stopped (received %d media chunks)", media_count)
                break

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.error("Error in media stream handler", exc_info=True)
    finally:
        if hello_task and not hello_task.done():
            hello_task.cancel()
        if pipeline_task and not pipeline_task.done():
            pipeline_task.cancel()
        # Finalize call record
        if transport and call_id:
            try:
                calls_dir = websocket.app.state.settings.config_dir / "harness" / "calls"
                result = transport.finalize_recording(calls_dir, call_id)
                rec_path = str(calls_dir / result[0]) if result else None
                await db.execute(
                    """UPDATE phone_calls
                       SET status = 'completed', completed_at = datetime('now'),
                           exchange_count = ?, recording_path = ?, duration_seconds = ?
                       WHERE id = ?""",
                    (exchange_index, rec_path, None, call_id),
                )
                await _emit_event(websocket.app.state, "phone.call.completed", {
                    "call_id": call_id,
                    "exchange_count": exchange_index,
                })
            except Exception:
                logger.warning("Failed to finalize call record", exc_info=True)
        logger.info("Twilio Media Stream disconnected")


async def _say_hello_if_silent(
    websocket: WebSocket,
    transport: Any,
    engines: Any,
    started_check: Any,
    has_speech_check: Any,
    call_id: str,
    exchange_index: int,
    db: Any,
) -> None:
    """After 5s of silence, play 'Hello?' to prompt the callee."""
    try:
        await asyncio.sleep(5.0)
        if started_check():
            return
        if has_speech_check():
            return
        logger.info("No speech after 5s, saying Hello?")
        t0 = time.monotonic()
        async with engines.tts.lock:
            audio, sr = await asyncio.to_thread(engines.tts.generate, "Hello?", "en")
        tts_ms = int((time.monotonic() - t0) * 1000)
        from cyborg_server.services.voice_engines import samples_to_wav
        wav_bytes = samples_to_wav(audio, sr)
        await transport.send_audio(wav_bytes)
        logger.info("Hello? played on phone call (TTS took %dms)", tts_ms)

        # Log as an exchange
        from datetime import datetime, timezone
        try:
            await db.execute(
                """INSERT INTO phone_call_exchanges
                   (call_id, exchange_index, user_transcript, assistant_transcript,
                    stt_ms, llm_total_ms, tts_first_chunk_ms, e2e_ms, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (call_id, exchange_index,
                 "(silence)", "Hello?",
                 None, None, tts_ms, tts_ms,
                 datetime.now(tz=timezone.utc).isoformat()),
            )
        except Exception:
            logger.warning("Failed to log hello exchange", exc_info=True)
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.warning("Failed to say hello", exc_info=True)


async def _run_voice_pipeline(
    websocket: WebSocket,
    transport: Any,
    wav_bytes: bytes,
    lock: asyncio.Lock,
    agenda: str,
    call_id: str,
    exchange_index: int,
    db: Any,
    ctx: Any,
    utterance_time: float = 0.0,
) -> None:
    """Run the voice pipeline for a single utterance and log the exchange."""
    proxy = _CallRecorderProxy(transport, call_id, exchange_index, db, utterance_time)
    async with lock:
        try:
            from cyborg_server.services.voice_service import VoiceService

            engines = websocket.app.state.voice_engines
            service = VoiceService(ctx, engines)

            await service.process_audio(
                transport=proxy,
                audio_chunks=[wav_bytes],
                language=None,
                user_id=f"phone:{call_id}",
                session_mode="chat",
                agenda=agenda,
            )
        except asyncio.CancelledError:
            logger.info("Voice pipeline cancelled for exchange %d (barge-in)", exchange_index)
            proxy.done.set()
            raise
        except Exception:
            logger.error("Voice pipeline error during phone call", exc_info=True)
            if not proxy._saved and (proxy.user_transcript or proxy.assistant_transcript):
                await proxy._save_exchange()
            proxy.done.set()

        # Wait for the full pipeline (LLM + TTS) to complete before releasing the lock,
        # preventing concurrent dispatches to the same session.
        if not proxy.done.is_set():
            try:
                await asyncio.wait_for(proxy.done.wait(), timeout=120)
            except asyncio.TimeoutError:
                logger.warning("Pipeline timed out for exchange %d", exchange_index)

    # Agent requested hangup via <hangup/> token — close the call
    if proxy.hangup_requested:
        logger.info("Agent requested hangup via <hangup/> token, closing call %s", call_id)
        try:
            await websocket.close()
        except Exception:
            pass
