"""Phone call integration via Twilio Media Streams.

Provides HTTP webhooks for Twilio call control and a WebSocket endpoint
for bidirectional audio streaming through the voice pipeline.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["phone"])


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


def _build_warmup_message(phone_context: str, agenda: str) -> str:
    prefix = (
        "[You are a voice assistant. Respond in plain spoken language: "
        "no emojis, no markdown formatting, no asterisks, no bullet points. Just natural speech.]"
    )
    full_agenda = f"{phone_context} {agenda}".strip() if agenda else phone_context
    prefix += (
        f" [CALL AGENDA: {full_agenda}. "
        "Follow this agenda throughout the conversation. "
        "Stay on topic and work toward the agenda's goal.]"
    )
    prefix += (
        " [NO_REPLY: This is a session warmup before the call connects. "
        "Do not generate a substantive response. Reply with a single period.]"
    )
    return prefix


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
                    stt_ms, openclaw_ms, tts_first_chunk_ms, e2e_ms,
                    gateway_prepare_ms, gateway_stream_ms, tts_wait_lock_ms, tts_generate_ms,
                    started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (self._call_id, self._exchange_index,
                 self.user_transcript, self.assistant_transcript,
                 self.latency.get("stt_ms"),
                 self.latency.get("openclaw_total_ms"),
                 self.latency.get("tts_first_chunk_ms"),
                 self.latency.get("e2e_ms"),
                 self.latency.get("gateway_prepare_ms"),
                 self.latency.get("gateway_stream_ms"),
                 self.latency.get("tts_wait_lock_ms"),
                 self.latency.get("tts_generate_ms"),
                 started_iso),
            )
        except Exception:
            logger.warning("Failed to log phone call exchange", exc_info=True)


async def _run_warmup(
    db: Any,
    settings: Any,
    session_key: str,
    warmup_message: str,
) -> bool:
    """Pre-warm the OpenClaw session. Returns True on success."""
    from cyborg_server.context import AppContext
    from cyborg_server.services.openclaw_hook_service import OpenClawHookService

    t0 = time.monotonic()
    try:
        ctx = AppContext(db=db, settings=settings)
        openclaw_svc = OpenClawHookService(ctx)

        try:
            await openclaw_svc.patch_session_model(session_key)
        except Exception:
            logger.warning("Warmup: model patch failed (non-fatal)", exc_info=True)

        coro = await openclaw_svc.prepare_streaming_agent_dispatch(
            message=warmup_message,
            session_key=session_key,
            idempotency_key=str(uuid4()),
        )
        await coro

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.info("Warmup succeeded for %s in %dms", session_key, elapsed_ms)
        return True
    except Exception:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.warning("Warmup failed for %s after %dms", session_key, elapsed_ms, exc_info=True)
        return False


@router.post("/call")
async def initiate_call(request: Request) -> dict:
    """Initiate an outbound phone call via Twilio."""
    body = await request.json()
    to_number = body.get("to", "").strip()
    if not to_number:
        return {"error": "Missing 'to' phone number"}

    agenda = body.get("agenda", "").strip()

    settings = request.app.state.settings.phone
    if not settings.enabled:
        return {"error": "Phone subsystem is not enabled"}

    # Generate call_id and session_key upfront for warmup
    call_id = str(uuid4())
    session_key = f"bobvoice:chat:phone:{call_id}"

    # Warm up the OpenClaw session before placing the call
    warmup_message = _build_warmup_message(_build_phone_context(), agenda)
    warmup_ok = await _run_warmup(
        db=request.app.state.db,
        settings=request.app.state.settings,
        session_key=session_key,
        warmup_message=warmup_message,
    )

    from twilio.rest import Client

    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    base_url = settings.base_url or request.app.state.settings.resolved_public_url

    call = client.calls.create(
        to=to_number,
        from_=settings.twilio_phone_number,
        url=f"{base_url}/phone/twiml",
        status_callback=f"{base_url}/phone/status",
        status_callback_event=["initiated", "ringing", "answered", "completed"],
    )

    # Store enriched data for retrieval during media stream
    if not hasattr(request.app.state, "call_agendas"):
        request.app.state.call_agendas = {}
    request.app.state.call_agendas[call.sid] = {
        "agenda": agenda,
        "phone_number": to_number,
        "call_id": call_id,
        "session_key": session_key,
        "warmup_done": warmup_ok,
    }

    logger.info("Initiated call %s to %s (warmup: %s)", call.sid, to_number, warmup_ok)
    return {"call_sid": call.sid, "call_id": call_id, "status": call.status}


@router.post("/twiml")
async def twiml_webhook(request: Request) -> PlainTextResponse:
    """Return TwiML that connects the call to our Media Stream WebSocket."""
    base_url = request.app.state.settings.phone.base_url or request.app.state.settings.resolved_public_url
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
    logger.info("Call %s status: %s", call_sid, call_status)

    # Clean up stored call data when call terminates
    if call_status in ("completed", "failed", "busy", "no-answer", "canceled"):
        agendas = getattr(request.app.state, "call_agendas", {})
        call_data = agendas.pop(call_sid, None)
        if call_data and isinstance(call_data, str):
            # Legacy format — just an agenda string
            pass

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
                  stt_ms, openclaw_ms, tts_first_chunk_ms, e2e_ms,
                  started_at, created_at
           FROM phone_call_exchanges
           WHERE call_id = ?
           ORDER BY exchange_index""",
        (call["id"],),
    )
    return {"call": dict(call), "exchanges": [dict(e) for e in exchanges]}


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
    warmup_ok = False

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
                agendas = getattr(websocket.app.state, "call_agendas", {})
                stored = agendas.get(call_sid, {})
                if isinstance(stored, dict):
                    raw_agenda = stored.get("agenda", "")
                    stored_phone = stored.get("phone_number", "")
                    call_id = stored.get("call_id", str(uuid4()))
                    warmup_ok = stored.get("warmup_done", False)
                else:
                    # Legacy format
                    raw_agenda = stored or ""
                    stored_phone = ""
                    call_id = str(uuid4())
                    warmup_ok = False
                phone_context = _build_phone_context()
                agenda = f"{phone_context} {raw_agenda}".strip() if raw_agenda else phone_context
                logger.info("Twilio stream started: %s (call: %s, warmup: %s)", stream_sid, call_sid, warmup_ok)

                # Create call record
                await db.execute(
                    """INSERT INTO phone_calls (id, call_sid, stream_sid, phone_number, direction, status, agenda, started_at)
                       VALUES (?, ?, ?, ?, 'outbound', 'active', ?, datetime('now'))""",
                    (call_id, call_sid, stream_sid, stored_phone, agenda),
                )

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

                # Only run silence detection when pipeline is idle
                if not processing_lock.locked():
                    transport.feed_inbound_audio(mulaw_bytes)

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
                                utterance_time, warmup_ok=warmup_ok,
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
                result = transport.finalize_recording(
                    websocket.app.state.settings.data_dir / "calls", call_id,
                )
                rec_path = result[0] if result else None
                rec_size = result[1] if result else None
                await db.execute(
                    """UPDATE phone_calls
                       SET status = 'completed', completed_at = datetime('now'),
                           exchange_count = ?, recording_path = ?, duration_seconds = ?
                       WHERE id = ?""",
                    (exchange_index, rec_path, None, call_id),
                )
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
                    stt_ms, openclaw_ms, tts_first_chunk_ms, e2e_ms, started_at)
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
    warmup_ok: bool = False,
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
                warmup_ok=warmup_ok,
            )
        except Exception:
            logger.error("Voice pipeline error during phone call", exc_info=True)
            if not proxy._saved and (proxy.user_transcript or proxy.assistant_transcript):
                await proxy._save_exchange()
            proxy.done.set()

        # Wait for the full pipeline (LLM + TTS) to complete before releasing the lock,
        # preventing concurrent dispatches to the same OpenClaw session.
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
