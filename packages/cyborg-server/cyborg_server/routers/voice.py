"""Voice chat WebSocket endpoint and static frontend serving."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from cyborg_server.services.voice_protocol import (
    ErrorMessage,
    HistoryEntry,
    HistoryMessage,
    StatusMessage,
    parse_client_message,
)
from cyborg_server.services.voice_service import VoiceService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["voice"])

_FRONTEND_DIR = Path(__file__).parent.parent / "voice_frontend"


def _get_engines(websocket: WebSocket) -> Any | None:
    """Return VoiceEngineManager or None if voice deps are unavailable."""
    engines = getattr(websocket.app.state, "voice_engines", None)
    if engines is None:
        logger.warning("Voice engines not loaded — voice dependencies may be missing")
    return engines


def _get_session_key(user_id: str, session_mode: str) -> str:
    return f"bobvoice:{session_mode}:{user_id}"


@router.websocket("/ws")
async def voice_websocket(websocket: WebSocket) -> None:
    await websocket.accept()
    client = websocket.client.host if websocket.client else "unknown"
    logger.info("Voice WS connected from %s", client)

    engines = _get_engines(websocket)
    if engines is None:
        try:
            await websocket.send_text(
                ErrorMessage(message="Voice is unavailable — dependencies not installed. Install with: pip install cyborg-server[voice]").model_dump_json()
            )
            await websocket.send_text(StatusMessage(state="idle").model_dump_json())
        except Exception:
            pass
        return

    audio_chunks: list[bytes] = []
    language: str | None = None
    user_id: str = "mike"
    session_mode: str = "chat"

    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break

            if "text" in msg:
                parsed = parse_client_message(msg["text"])
                if parsed is None:
                    continue

                match parsed:
                    case parsed if parsed.type == "start_recording":
                        audio_chunks = []
                        language = parsed.language
                        user_id = parsed.userId
                        session_mode = parsed.sessionMode

                    case parsed if parsed.type == "stop_recording":
                        from cyborg_server.context import AppContext

                        app = websocket.app
                        ctx = AppContext(db=app.state.db, settings=app.state.settings, voice_engines=engines)
                        service = VoiceService(ctx, engines)
                        await service.process_audio(websocket, audio_chunks, language, user_id, session_mode)

                    case parsed if parsed.type == "cancel":
                        audio_chunks = []

                    case parsed if parsed.type == "set_language":
                        language = parsed.language

                    case parsed if parsed.type == "session_history":
                        from cyborg_server.services.voice_session_store import VoiceSessionStore
                        from cyborg_server.context import AppContext

                        app = websocket.app
                        ctx = AppContext(db=app.state.db, settings=app.state.settings)
                        store = VoiceSessionStore(ctx)
                        key = _get_session_key(parsed.userId, parsed.sessionMode)
                        messages = await store.get_messages(key)
                        await websocket.send_text(
                            HistoryMessage(
                                messages=[HistoryEntry(role=e["role"], text=e["text"], language=e.get("language")) for e in messages]
                            ).model_dump_json()
                        )

                    case parsed if parsed.type == "clear_history":
                        from cyborg_server.services.voice_session_store import VoiceSessionStore
                        from cyborg_server.context import AppContext

                        app = websocket.app
                        ctx = AppContext(db=app.state.db, settings=app.state.settings)
                        store = VoiceSessionStore(ctx)
                        key = _get_session_key(parsed.userId, parsed.sessionMode)
                        await store.delete_session(key)
                        await store.reset_all_lessons(parsed.userId, parsed.sessionMode)

                    case parsed if parsed.type == "replay_tts":
                        from cyborg_server.context import AppContext

                        app = websocket.app
                        ctx = AppContext(db=app.state.db, settings=app.state.settings, voice_engines=engines)
                        service = VoiceService(ctx, engines)
                        default_lang = "en" if parsed.sessionMode == "beginner_french" else (language or "en")
                        await service.replay_tts(websocket, parsed.text, default_lang)

            elif "bytes" in msg:
                audio_chunks.append(msg["bytes"])

    except WebSocketDisconnect:
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
