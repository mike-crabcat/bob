"""Realtime voice WebSocket endpoint and static frontend serving.

The legacy local STT→LLM→TTS pipeline (/voice/ws, voice_service,
voice_engines, language-practice frontend) was removed in Bob3 Phase 0;
`voice_lesson_progress` data is preserved in the database. All voice runs
through the OpenAI Realtime bridge below.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from server.services.realtime_bridge import BrowserAudioSource, RealtimeBridge
from server.services.realtime_tools import make_realtime_tools

logger = logging.getLogger(__name__)

router = APIRouter(tags=["voice"])


_FRONTEND_DIR = Path(__file__).parent.parent / "voice_frontend"


@router.websocket("/realtime")
async def voice_realtime(websocket: WebSocket) -> None:
    """Browser test harness for the OpenAI Realtime bridge.

    Protocol:
    - First text frame (JSON): ``{type:"start", instructions, voice?, max_duration?, phone_number?}``
    - Subsequent binary frames: PCM16 LE 24kHz mono mic audio
    - Text control frames: ``{type:"stop"}`` to end the session
    - Server sends: binary speaker audio, ``{type:"transcript_delta",text}``,
      ``{type:"barge_in"}``, and a final ``{type:"done", ...}`` with the result.

    This runs the same RealtimeBridge the phone path uses, so behaviour here
    predicts phone behaviour — iterate on prompts/voice/tools without calls.
    """
    await websocket.accept()
    client = websocket.client.host if websocket.client else "unknown"
    logger.info("Realtime WS connected from %s", client)

    from server.context import AppContext
    ctx = AppContext(
        db=websocket.app.state.db,
        settings=websocket.app.state.settings,
        event_bus=websocket.app.state.event_bus,
    )
    rt_settings = ctx.settings.openai_realtime
    api_key = ctx.settings.openai.api_key

    if not api_key:
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": "OpenAI API key not configured"}))
        except Exception:
            pass
        return

    try:
        first = await websocket.receive_text()
        config = json.loads(first)
    except (json.JSONDecodeError, WebSocketDisconnect):
        return
    if config.get("type") != "start":
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": "expected {type:'start', ...}"}))
        except Exception:
            pass
        return

    # Two start modes:
    #   - test mode: {instructions, voice?, max_duration?}  (manual prompt; no session record)
    #   - persona mode: {session_id}  (Bob-initiated; loads persona + chat context)
    session_id = config.get("session_id")

    on_complete: Any = None  # set below in persona mode
    session_svc = None  # VoiceSessionService in persona mode
    if session_id:
        from server.services.voice_session_service import VoiceSessionService
        session_svc = VoiceSessionService(ctx)
        row = await session_svc.resolve(session_id)
        if row is None:
            try:
                await websocket.send_text(json.dumps({"type": "error", "message": "voice session link is invalid, used, or expired"}))
            except Exception:
                pass
            return
        instructions = await session_svc.build_instructions(row)
        voice = row["voice"] or rt_settings.voice
        # The agent NEVER gets end_call — it hangs up on the user prematurely every
        # time. The human ends the call by tapping hang-up. Goal mode keeps the
        # outcome tools (report_success/report_failure) so the agent can capture
        # results; persona mode has no tools at all.
        if (row.get("goal") or "").strip():
            all_tools = make_realtime_tools(ctx, phone_number="")
            tools = [t for t in all_tools if t.name in ("report_success", "report_failure")]
        else:
            tools = []
        max_duration = rt_settings.max_call_duration_seconds
        wa_service = getattr(websocket.app.state, "whatsapp_bridge_service", None)

        async def on_complete(transcript: str, duration: float, tool_calls: list | None = None) -> None:
            await session_svc.complete(
                session_id, transcript, duration,
                tool_calls=tool_calls, wa_service=wa_service,
            )
    else:
        if not config.get("instructions"):
            try:
                await websocket.send_text(json.dumps({"type": "error", "message": "expected {type:'start', instructions:...} or {type:'start', session_id:...}"}))
            except Exception:
                pass
            return
        instructions = config["instructions"]
        voice = config.get("voice") or rt_settings.voice
        tools = make_realtime_tools(ctx, phone_number=config.get("phone_number", ""))
        max_duration = min(
            float(config.get("max_duration") or rt_settings.max_call_duration_seconds),
            rt_settings.max_call_duration_seconds,
        )

    source = BrowserAudioSource(websocket)

    async def emit(event_name: str, payload: dict) -> None:
        await source.send_control({"type": event_name, **payload})

    # Persist the partial transcript after every turn boundary so the dashboard
    # and DB queries can see progress while the call is still live — and so we
    # don't lose everything if the bridge hangs on cleanup (observed 2026-08-13).
    voice_session_id_for_turn = session_id  # closure capture, may be None in test mode

    async def on_turn(transcript: str) -> None:
        if session_svc is None:
            return
        await session_svc.persist_transcript(voice_session_id_for_turn, transcript)

    bridge = RealtimeBridge(
        source,
        api_key=api_key,
        model=rt_settings.model,
        instructions=instructions,
        voice=voice,
        tools=tools,
        max_duration_seconds=max_duration,
        turn_detection=rt_settings.turn_detection,
        emit=emit,
        on_turn=on_turn,
        # Persona mode (voice-link sessions): same phone-call convention as
        # outbound calls — the person who tapped the link gets the first turn,
        # and the opening gate cancels noise-triggered turns. Test mode keeps
        # agent-first (it exists to iterate on agent-opening tasks).
        speak_first=session_id is None,
    )

    async def ws_read_loop() -> None:
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    source.feed_frame(None)
                    return
                if "bytes" in msg and msg["bytes"]:
                    source.feed_frame(msg["bytes"])
                elif "text" in msg:
                    try:
                        ctl = json.loads(msg["text"])
                    except json.JSONDecodeError:
                        continue
                    if ctl.get("type") == "stop":
                        source.feed_frame(None)
                        return
        except WebSocketDisconnect:
            source.feed_frame(None)
        except Exception:
            logger.warning("Realtime WS read loop error", exc_info=True)
            source.feed_frame(None)

    async def bridge_task() -> None:
        result = await bridge.run()
        try:
            await websocket.send_text(json.dumps({
                "type": "done",
                "transcript": result.transcript,
                "duration_seconds": round(result.duration_seconds, 2),
                "tool_calls": result.tool_calls,
                "end_reason": result.end_reason,
                "error_message": result.error_message,
            }))
        except Exception:
            pass
        if on_complete is not None:
            try:
                await on_complete(result.transcript, result.duration_seconds, result.tool_calls)
            except Exception:
                logger.warning("voice session on_complete failed", exc_info=True)

    read_t = asyncio.create_task(ws_read_loop())
    bridge_t = asyncio.create_task(bridge_task())
    done, pending = await asyncio.wait({read_t, bridge_t}, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass


@router.post("/log")
async def client_log(request: Request) -> JSONResponse:
    body = await request.json()
    level = body.get("level", "info")
    message = body.get("message", "")
    tag = body.get("tag", "frontend")
    context = body.get("context")
    log_fn = logger.warning if level == "error" else logger.info
    extra = f" — {context}" if context else ""
    log_fn("[%s] %s%s", tag, message, extra)
    return JSONResponse({"ok": True})


def mount_frontend(app: FastAPI, frontend_dir: Path | None = None) -> None:
    """Mount the voice frontend SPA as static files under /voice/.

    Must be called after the voice router is included so that /voice/ws
    and /voice/log take precedence over the static catch-all.
    """
    directory = frontend_dir or _FRONTEND_DIR
    if not directory.is_dir():
        logger.warning("Voice frontend directory not found: %s", directory)
        return
    app.mount("/voice", StaticFiles(directory=str(directory), html=True), name="voice_frontend")
    logger.info("Voice frontend mounted from %s", directory)
