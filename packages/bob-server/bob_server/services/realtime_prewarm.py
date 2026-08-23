"""Prewarmed OpenAI Realtime sessions for outbound phone calls.

The greeting-miss problem (2026-08-17): the OpenAI session was only created
AFTER the Twilio call was answered — two serialized network handshakes
(Twilio→us, us→OpenAI) during which the callee's "hello" queued locally and
was later dumped as a faster-than-real-time burst into a half-configured
session. Server VAD unreliably committed speech delivered that way, so the
greeting often vanished: no turn, no transcript, no reply.

Fix: create and fully configure the session (connect + session.update +
session.updated confirmation — see ``RealtimeBridge.prewarm``) while the
phone is still RINGING, then hand it to the media-stream handler at answer.
Ringing gives 5–30s of free time vs ~2s of setup, so at answer the session
is live and audio flows at 1× from the first frame.

Lifecycle:
- ``start_prewarm`` at placement (keyed by our call_id, known before Twilio
  returns a call sid).
- ``claim`` in the media handler; one-shot. Returns None on any failure —
  the handler falls back to connecting at answer, the pre-prewarm behaviour.
- ``discard`` on terminal call statuses, plus a TTL sweeper as backstop for
  missed webhooks (no-answer calls must not leak idle sessions).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Ring timeout is typically ≤ 60s; 120s covers slow test lines. Unclaimed
# sessions past this are closed — an idle realtime session costs pennies
# but should never outlive its call.
_PREWARM_TTL_SECONDS = 120.0


class _Entry:
    __slots__ = ("bridge", "ready", "expiry")

    def __init__(self, bridge: Any, ready: asyncio.Task) -> None:
        self.bridge = bridge
        self.ready = ready
        self.expiry: asyncio.Task | None = None

    def cancel_expiry(self) -> None:
        if self.expiry and not self.expiry.done():
            self.expiry.cancel()


_sessions: dict[str, _Entry] = {}


async def _prepare(bridge: Any) -> None:
    try:
        await bridge.prewarm()
    except asyncio.CancelledError:
        await bridge.abandon()
        raise
    except Exception:
        # prewarm() already abandoned its socket; claim() sees the failure
        # via the task and falls back to connect-at-answer.
        raise


def start_prewarm(
    call_id: str,
    *,
    db: Any,
    settings: Any,
    phone_number: str,
    agenda: str,
    meta: dict | None,
) -> None:
    """Create + configure a Realtime session for an outbound call, to be
    claimed by the media handler at answer. Fire-and-forget: every failure
    path degrades to the old connect-at-answer behaviour."""
    from bob_server.context import AppContext
    from bob_server.services.realtime_bridge import RealtimeBridge
    from bob_server.services.realtime_tools import make_realtime_tools

    rt = settings.openai_realtime
    meta = meta or {}
    # Mirror _run_realtime_call's bridge construction exactly — the session
    # config (instructions/voice/tools) must match what it would have sent.
    ctx = AppContext(db=db, settings=settings)
    tools = make_realtime_tools(ctx, phone_number=phone_number)
    instructions = meta.get("instructions") or agenda
    voice = meta.get("voice") or rt.voice
    max_duration = min(
        float(meta.get("max_duration") or rt.max_call_duration_seconds),
        rt.max_call_duration_seconds,
    )

    async def on_turn(transcript: str) -> None:
        from bob_server.services.voice_dispatch_service import persist_call_transcript
        await persist_call_transcript(db, call_id, transcript)

    # Outbound calls are callee-first; the audio source is attached when the
    # media stream claims the session.
    bridge = RealtimeBridge(
        None,
        api_key=settings.openai.api_key,
        model=rt.model,
        instructions=instructions,
        voice=voice,
        tools=tools,
        max_duration_seconds=max_duration,
        turn_detection=rt.turn_detection,
        on_turn=on_turn,
        speak_first=False,
    )
    entry = _Entry(bridge, asyncio.create_task(_prepare(bridge)))
    entry.expiry = asyncio.create_task(_expire_later(call_id, _PREWARM_TTL_SECONDS))
    _sessions[call_id] = entry
    logger.info("Prewarming realtime session for call %s while ringing", call_id)


async def claim(call_id: str, *, wait: float = 3.0) -> Any | None:
    """Hand a prewarmed session to the media handler. One-shot; returns None
    (and cleans up) if prewarm failed, is still connecting after `wait`
    seconds (callee answered within the setup window), or the socket died
    during a long ring."""
    entry = _sessions.pop(call_id, None)
    if entry is None:
        return None
    entry.cancel_expiry()
    try:
        await asyncio.wait_for(asyncio.shield(entry.ready), wait)
    except Exception:
        entry.ready.cancel()
        await entry.bridge.abandon()
        logger.info("Prewarm for call %s not ready in time — falling back", call_id)
        return None
    if not await entry.bridge.verify_prewarmed():
        await entry.bridge.abandon()
        logger.info("Prewarm for call %s failed liveness check — falling back", call_id)
        return None
    logger.info("Claimed prewarmed realtime session for call %s", call_id)
    return entry.bridge


async def discard(call_id: str) -> None:
    """Drop an unclaimed prewarmed session (call failed / never answered)."""
    entry = _sessions.pop(call_id, None)
    if entry is None:
        return
    entry.cancel_expiry()
    entry.ready.cancel()
    await entry.bridge.abandon()


async def _expire_later(call_id: str, ttl: float) -> None:
    await asyncio.sleep(ttl)
    entry = _sessions.pop(call_id, None)
    if entry is None:
        return
    entry.cancel_expiry()
    entry.ready.cancel()
    await entry.bridge.abandon()
    logger.info("Discarded unclaimed prewarmed session for call %s (TTL)", call_id)
