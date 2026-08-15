"""OpenAI Realtime API voice bridge.

Audio-source-agnostic bridge between a caller (Twilio phone call or browser
test harness) and the OpenAI Realtime API. The same bridge core serves both
paths — only the :class:`AudioSource` implementation differs:

- :class:`TwilioMediaSource` — μ-law 8kHz over a Twilio Media Stream WebSocket
- :class:`BrowserAudioSource` — PCM16 24kHz over a browser WebSocket

This lets you iterate on prompt/voice/tool behaviour in the browser (free,
instant) and reuse the exact same code path for real phone calls.

See plans/snoopy-leaping-crystal.md for the full design.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import websockets

from bob_server.services.mulaw import (
    AntiAliasedDownsampler,
    apply_gain,
    mulaw_to_pcm16,
    pcm16_to_mulaw,
    resample_8k_to_24k,
)
from bob_server.services.tools import ImageInjection, Tool

logger = logging.getLogger(__name__)

# PCM16 (16-bit) = 2 bytes/sample; OpenAI Realtime audio is mono PCM16 LE at 24 kHz.
_SAMPLE_BYTES = 2

# Twilio Media Stream sends 20ms frames (160 μ-law bytes at 8kHz).
_TWILIO_FRAME_MS = 0.02


@runtime_checkable
class AudioSource(Protocol):
    """Abstract audio I/O the bridge talks to.

    All audio exchanged with the bridge is PCM16 little-endian mono at 24 kHz —
    OpenAI's native format. Implementations handle any format conversion to
    their transport (Twilio μ-law, browser raw PCM).
    """

    async def recv_mic_pcm16_24k(self) -> bytes | None:
        """Return the next mic audio chunk as PCM16 24kHz bytes, or None when the source has closed."""
        ...

    async def send_speaker_pcm16_24k(self, chunk: bytes) -> None:
        """Play a PCM16 24kHz chunk to the caller."""
        ...

    async def clear_playback(self) -> None:
        """Drop any queued playback (barge-in)."""
        ...

    def input_closed(self) -> bool:
        """Cheap sync check: has the caller/mic side closed?"""
        ...

    async def aclose(self) -> None:
        """Release transport resources."""
        ...


@dataclass
class BridgeResult:
    """Outcome of a Realtime session, returned by :meth:`RealtimeBridge.run`."""

    transcript: str = ""
    duration_seconds: float = 0.0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    end_reason: str = ""  # "completed" | "input_closed" | "end_tool" | "timeout" | "error"
    error_message: str = ""


class TwilioMediaSource:
    """AudioSource backed by a Twilio Media Stream WebSocket.

    The phone media handler pushes inbound μ-law frames via
    :meth:`feed_inbound_mulaw` and signals stop via :meth:`signal_closed`. A
    background task drains queued outbound audio, converts to μ-law, and paces
    20ms frames to Twilio.
    """

    def __init__(self, ws: Any, stream_sid: str, *, inbound_gain: float = 4.0) -> None:
        self._ws = ws
        self._stream_sid = stream_sid
        self._inbound: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._outbound: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._send_task: asyncio.Task[None] | None = None
        self._interrupt = False
        self._closed = False
        # Inbound boost (with clip protection): phone audio arrives very quiet
        # (observed ~-38 dB RMS), which hurts both ASR and recordings.
        self.inbound_gain = inbound_gain
        # Stateful anti-aliased 24k→8k downsampler (kept across chunks).
        self._downsampler = AntiAliasedDownsampler(factor=3, sample_rate=24000.0)
        # Recording taps (PCM16 24kHz) for stereo capture, both as
        # (wallclock_seconds, chunk). Correct because both sides are paced in
        # real time: inbound by Twilio's 20ms frames, outbound by our pacing
        # loop (each frame takes ≥20ms wall to send, so sends can never
        # overlap). Overlaying OpenAI's raw deltas by ARRIVAL time was the
        # original garble bug — they arrive in bursts faster than real time.
        self.rec_inbound_pcm24: list[tuple[float, bytes]] = []
        self.rec_outbound_pcm24: list[tuple[float, bytes]] = []
        self._start_monotonic: float = 0.0

    async def start(self) -> None:
        self._start_monotonic = time.monotonic()
        self._send_task = asyncio.create_task(self._outbound_loop())

    def feed_inbound_mulaw(self, mulaw_bytes: bytes) -> None:
        """Called from the Twilio WS read loop for each media frame."""
        if not self._closed:
            self._inbound.put_nowait(mulaw_bytes)

    def signal_closed(self) -> None:
        self._closed = True
        self._inbound.put_nowait(None)
        self._outbound.put_nowait(None)

    async def recv_mic_pcm16_24k(self) -> bytes | None:
        mulaw = await self._inbound.get()
        if mulaw is None:
            return None
        pcm8k = mulaw_to_pcm16(mulaw)
        pcm24k = resample_8k_to_24k(pcm8k)
        if self.inbound_gain != 1.0:
            pcm24k = apply_gain(pcm24k, self.inbound_gain)
        chunk = pcm24k.tobytes()
        if self._start_monotonic:
            self.rec_inbound_pcm24.append((time.monotonic() - self._start_monotonic, chunk))
        return chunk

    async def send_speaker_pcm16_24k(self, chunk: bytes) -> None:
        await self._outbound.put(chunk)

    async def clear_playback(self) -> None:
        self._interrupt = True
        while not self._outbound.empty():
            try:
                self._outbound.get_nowait()
            except asyncio.QueueEmpty:
                break

    def input_closed(self) -> bool:
        return self._closed

    async def _outbound_loop(self) -> None:
        """Drain outbound PCM24, convert to μ-law 8kHz, pace 20ms frames to Twilio."""
        import numpy as np
        try:
            while True:
                chunk = await self._outbound.get()
                if chunk is None:
                    return
                self._interrupt = False
                pcm24 = np.frombuffer(chunk, dtype=np.int16)
                pcm8 = self._downsampler.process(pcm24)
                mulaw = pcm16_to_mulaw(pcm8)
                # 160 μ-law bytes == 20ms at 8kHz; each corresponds to 480
                # PCM24 samples (24k = 3 × 8k) — recorded for the call WAV.
                interrupted = False
                for i in range(0, len(mulaw), 160):
                    if self._interrupt:
                        interrupted = True
                        break
                    frame = mulaw[i : i + 160]
                    payload = base64.b64encode(frame).decode("ascii")
                    msg = json.dumps({
                        "event": "media",
                        "streamSid": self._stream_sid,
                        "media": {"payload": payload},
                    })
                    try:
                        await self._ws.send_text(msg)
                    except Exception as e:
                        logger.warning("TwilioMediaSource send failed: %s", e)
                        return
                    if self._start_monotonic:
                        self.rec_outbound_pcm24.append(
                            (time.monotonic() - self._start_monotonic,
                             pcm24[i * 3 : i * 3 + len(frame) * 3].tobytes())
                        )
                    await asyncio.sleep(_TWILIO_FRAME_MS)
                if interrupted:
                    # Barge-in: drop the rest of this utterance (and anything
                    # still queued from it) but KEEP THE LOOP ALIVE — the next
                    # agent response still needs a consumer. Returning here
                    # used to kill outbound audio for the rest of the call.
                    logger.info("TwilioMediaSource: barge-in, dropping interrupted utterance")
                    while not self._outbound.empty():
                        try:
                            self._outbound.get_nowait()
                        except asyncio.QueueEmpty:
                            break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("TwilioMediaSource outbound loop error", exc_info=True)

    async def aclose(self) -> None:
        if self._send_task and not self._send_task.done():
            self._send_task.cancel()
            try:
                await self._send_task
            except (asyncio.CancelledError, Exception):
                pass

    def finalize_recording(self, calls_dir: Path, call_id: str) -> tuple[str, int] | None:
        """Write a stereo WAV (left=inbound, right=outbound) at 24kHz.

        Both channels are overlaid at their wall-clock send/receive times, so
        the two sides stay time-aligned across the whole call.
        """
        import numpy as np
        import soundfile as sf

        if not self.rec_inbound_pcm24 and not self.rec_outbound_pcm24:
            return None

        target_sr = 24000

        def _end_sample(taps: list[tuple[float, bytes]]) -> int:
            last_t, last_chunk = taps[-1]
            return int((last_t + len(last_chunk) / (target_sr * _SAMPLE_BYTES)) * target_sr)

        total_samples = 0
        if self.rec_inbound_pcm24:
            total_samples = max(total_samples, _end_sample(self.rec_inbound_pcm24))
        if self.rec_outbound_pcm24:
            total_samples = max(total_samples, _end_sample(self.rec_outbound_pcm24))
        if total_samples <= 0:
            return None

        def _overlay(taps: list[tuple[float, bytes]]) -> np.ndarray:
            ch = np.zeros(total_samples, dtype=np.float32)
            for ts, chunk in taps:
                pcm = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
                offset = int(ts * target_sr)
                end_idx = min(offset + len(pcm), total_samples)
                if offset < total_samples and end_idx > offset:
                    ch[offset:end_idx] = pcm[: end_idx - offset]
            return ch

        stereo = np.column_stack([
            _overlay(self.rec_inbound_pcm24),
            _overlay(self.rec_outbound_pcm24),
        ])
        calls_dir.mkdir(parents=True, exist_ok=True)
        path = calls_dir / f"{call_id}.wav"
        sf.write(str(path), stereo, target_sr, subtype="PCM_16", format="WAV")
        self.rec_inbound_pcm24.clear()
        self.rec_outbound_pcm24.clear()
        return (path.name, path.stat().st_size)


class BrowserAudioSource:
    """AudioSource backed by a browser WebSocket using raw PCM16 24kHz.

    The browser sends mic audio as binary frames (PCM16 LE 24kHz mono) and
    receives speaker audio the same way. JSON text frames carry control
    messages (``barge_in`` notification, transcript captions, ``done``).
    """

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._inbound: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._closed = False
        self._on_transcript: Any = None  # optional callback(text)

    def feed_frame(self, data: bytes | None) -> None:
        """Called from the browser WS loop: bytes = mic audio, None = closed."""
        if data is None:
            self._closed = True
        self._inbound.put_nowait(data)

    async def recv_mic_pcm16_24k(self) -> bytes | None:
        return await self._inbound.get()

    async def send_speaker_pcm16_24k(self, chunk: bytes) -> None:
        try:
            await self._ws.send_bytes(chunk)
        except Exception:
            pass

    async def clear_playback(self) -> None:
        try:
            await self._ws.send_text(json.dumps({"type": "barge_in"}))
        except Exception:
            pass

    async def send_control(self, payload: dict) -> None:
        try:
            await self._ws.send_text(json.dumps(payload))
        except Exception:
            pass

    def input_closed(self) -> bool:
        return self._closed

    async def aclose(self) -> None:
        pass


class RealtimeBridge:
    """Owns one OpenAI Realtime WebSocket and relays audio + events to/from an AudioSource."""

    def __init__(
        self,
        audio_source: AudioSource,
        *,
        api_key: str,
        model: str,
        instructions: str,
        voice: str,
        tools: list[Tool] | None = None,
        max_duration_seconds: float = 300.0,
        turn_detection: str = "server_vad",
        base_url: str = "wss://api.openai.com/v1/realtime",
        emit: Any = None,
        on_turn: Any = None,
        speak_first: bool = True,
        opening_listen_seconds: float = 5.0,
    ) -> None:
        self.audio = audio_source
        self.api_key = api_key
        self.model = model
        self.instructions = instructions
        self.voice = voice
        self.tools = tools or []
        self.max_duration_seconds = max_duration_seconds
        self.turn_detection = turn_detection
        self.base_url = base_url
        self.emit = emit  # optional async callback(event_name, payload) for UI/captions
        # Optional async callback(full_transcript: str) fired after each turn boundary
        # (assistant response.done or user transcript completed). Lets the caller
        # persist partial transcript so dashboard/monitoring can see progress even
        # if the bridge hangs on cleanup.
        self.on_turn = on_turn
        # Phone-call convention: on an outbound call the CALLEE speaks first
        # (typically "hello"). When speak_first is False the bridge does not
        # prompt an opening response on connect; the agent's first turn comes
        # from server VAD replying to the callee's greeting.
        #
        # Opening gate: VAD can fire on connection noise and create a response
        # with NO transcribed human speech behind it (observed 2026-08-15: a
        # noise burst at connect made the agent open with "Hi Sophia"). During
        # the first opening_listen_seconds, any response created without a
        # completed user transcription is cancelled and its audio suppressed;
        # at the end of the window the agent is prompted to open only if
        # nothing conversational happened.
        self.speak_first = speak_first
        self.opening_listen_seconds = opening_listen_seconds

        self._tool_handlers: dict[str, Tool] = {t.name: t for t in self.tools}
        self._end_requested = asyncio.Event()
        self._session_ready = asyncio.Event()
        self._first_speech = asyncio.Event()
        # Opening-gate state (callee-first mode).
        self._grace_until = 0.0  # monotonic deadline; 0 = no gate (speak_first)
        self._user_transcript_seen = False
        self._response_in_flight = False
        self._conversational_response_done = False
        self._suppressed_response_ids: set[str] = set()
        self._timed_out = False
        self._last_error = ""
        # Diagnostic counters (logged at session end).
        self._mic_chunks_sent = 0
        self._speech_started_count = 0
        self._response_count = 0
        # Turn-ordered transcript: blocks like "Agent: ..." / "User: ..." in order.
        self._turns: list[str] = []
        self._current_agent: str = ""  # accumulating assistant transcript for the current turn
        self._tool_calls: list[dict[str, Any]] = []

    async def run(self) -> BridgeResult:
        """Connect, configure the session, relay audio, return the result."""
        start = time.monotonic()
        uri = f"{self.base_url}?model={self.model}"
        # GA Realtime API: no OpenAI-Beta header (sending it routes to the
        # legacy beta shape, which is now disabled → invalid_request_error.beta_api_shape_disabled).
        headers = {"Authorization": f"Bearer {self.api_key}"}
        result = BridgeResult()

        try:
            async with websockets.connect(uri, additional_headers=headers) as oai:
                await self._send_session_update(oai)

                relay = asyncio.create_task(self._relay_mic_to_openai(oai))
                events = asyncio.create_task(self._event_loop(oai))
                timer = asyncio.create_task(self._duration_timer())
                # Detached (not in the wait set — its completion must not end
                # the session): nudges the agent to open if the callee never
                # speaks within the delay window.
                nudge = None
                if not self.speak_first:
                    nudge = asyncio.create_task(self._first_response_nudge(oai))

                done, pending = await asyncio.wait(
                    {relay, events, timer, asyncio.create_task(self._end_requested.wait())},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # Which task triggered the end? (diagnostic)
                ended_by = "unknown"
                for t in done:
                    if t is relay:
                        ended_by = "mic_relay_ended (browser WS dropped or stop sent)"
                    elif t is events:
                        ended_by = "openai_ws_closed"
                    elif t is timer:
                        ended_by = "max_duration_timer"
                    else:
                        ended_by = "end_requested"
                for t in pending:
                    t.cancel()
                if nudge is not None and not nudge.done():
                    nudge.cancel()
                    pending.add(nudge)
                # Await the cancelled pending tasks so they fully unwind before
                # the `async with websockets.connect` exits. Without this the
                # events loop can be mid-`async for raw in oai:` when the WS
                # close runs, and the close hangs waiting for the receive loop
                # to release the connection — which never happens. Observed
                # hung bridge on max-duration timeout, 2026-08-13.
                for t in pending:
                    try:
                        await asyncio.wait_for(t, timeout=2.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                        pass
                for t in done:
                    exc = t.exception()
                    if exc and not isinstance(exc, (asyncio.CancelledError,)):
                        logger.warning("Realtime bridge task error: %s", exc)

                if self._end_requested.is_set():
                    result.end_reason = "end_tool"
                elif self._timed_out:
                    result.end_reason = "timeout"
                elif self.audio.input_closed():
                    result.end_reason = "input_closed"
                else:
                    result.end_reason = "completed"

                logger.info(
                    "Bridge session ending: reason=%s ended_by=%s | responses=%d speech_started=%d mic_sent=%d",
                    result.end_reason, ended_by, self._response_count,
                    self._speech_started_count, self._mic_chunks_sent,
                )
        except asyncio.TimeoutError:
            result.end_reason = "timeout"
        except asyncio.CancelledError:
            # External cancellation (e.g. caller's WS read loop ended because
            # the user hung up). Don't propagate — if we do, bridge_task()'s
            # `result = await bridge.run()` raises and on_complete never runs,
            # so the transcript is never persisted and the parent session
            # never gets the call-result dispatch. Treat as a normal end.
            result.end_reason = "cancelled"
            logger.info("Bridge cancelled by caller (end_reason=cancelled)")
        except Exception as e:
            logger.warning("Realtime bridge error: %s", e, exc_info=True)
            result.end_reason = "error"
            result.error_message = str(e)
        finally:
            try:
                await self.audio.aclose()
            except Exception:
                pass

        # Flush any in-flight assistant transcript at the end.
        if self._current_agent.strip():
            self._turns.append(f"Agent: {self._current_agent.strip()}")
        result.transcript = "\n".join(self._turns).strip()
        result.tool_calls = self._tool_calls
        result.duration_seconds = time.monotonic() - start
        if not result.error_message and self._last_error:
            result.error_message = self._last_error
        return result

    async def _duration_timer(self) -> None:
        await asyncio.sleep(self.max_duration_seconds)
        self._timed_out = True
        logger.info("Realtime bridge: max duration %ss reached", self.max_duration_seconds)

    async def _first_response_nudge(self, oai: websockets.WebSocketClientProtocol) -> None:
        """Opening gate: after the listen window, make sure the agent takes a
        turn — prompt it to open if nothing conversational happened (dead line,
        or the greeting's response was cancelled as noise-triggered)."""
        await self._session_ready.wait()
        await asyncio.sleep(self.opening_listen_seconds)
        if self._end_requested.is_set():
            return
        if self._response_in_flight or self._conversational_response_done:
            return
        logger.info(
            "Bridge: opening window (%.0fs) elapsed without a conversational turn — prompting agent",
            self.opening_listen_seconds,
        )
        await self._send(oai, {"type": "response.create"})

    async def request_end(self) -> None:
        """Signal the bridge to wind down (e.g. ``end_call`` tool)."""
        self._end_requested.set()

    async def _send_session_update(self, oai: websockets.WebSocketClientProtocol) -> None:
        # GA nests audio config under session.audio. The flat beta layout is
        # the fallback if the model rejects this.
        session = {
            "type": "realtime",
            "instructions": self.instructions,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {"model": "gpt-realtime-whisper"},
                    "noise_reduction": {"type": "far_field"},
                    "turn_detection": {
                        "type": self.turn_detection,
                        "threshold": 0.75,
                        "silence_duration_ms": 500,
                        "prefix_padding_ms": 300,
                        "interrupt_response": False,
                    },
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "voice": self.voice,
                },
            },
            "tools": [t.to_openai_format() for t in self.tools],
            "tool_choice": "auto",
            "reasoning": {"effort": "low"},
        }
        logger.info(
            "Bridge: sending session.update | model=%s voice=%s tools=%s reasoning=low\n"
            "=== INSTRUCTIONS START ===\n%s\n=== INSTRUCTIONS END ===",
            self.model, self.voice, [t.name for t in self.tools], self.instructions,
        )
        await self._send(oai, {"type": "session.update", "session": session})

    async def _relay_mic_to_openai(self, oai: websockets.WebSocketClientProtocol) -> None:
        while True:
            chunk = await self.audio.recv_mic_pcm16_24k()
            if chunk is None:
                return
            if not chunk:
                continue
            self._mic_chunks_sent += 1
            await self._send(oai, {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(chunk).decode("ascii"),
            })

    async def _event_loop(self, oai: websockets.WebSocketClientProtocol) -> None:
        async for raw in oai:
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            etype = event.get("type", "")
            try:
                await self._dispatch_event(oai, etype, event)
            except Exception:
                logger.warning("Realtime event dispatch failed for %s", etype, exc_info=True)
            if self._end_requested.is_set():
                return

    async def _dispatch_event(self, oai: websockets.WebSocketClientProtocol, etype: str, event: dict) -> None:
        if etype == "session.updated":
            self._session_ready.set()
            if self.speak_first:
                # Agent opens the task.
                await self._send(oai, {"type": "response.create"})
            else:
                # Callee-first: arm the opening gate.
                self._grace_until = time.monotonic() + self.opening_listen_seconds
            return

        if etype == "response.created":
            self._response_count += 1
            self._response_in_flight = True
            response_id = (event.get("response") or {}).get("id") or ""
            within_grace = self._grace_until and time.monotonic() < self._grace_until
            if within_grace and not self._user_transcript_seen:
                # A response with no transcribed human speech behind it during
                # the opening window = VAD fired on connection noise. Kill it
                # before any audio reaches the caller.
                logger.info(
                    "Bridge: cancelling noise-triggered response #%d during opening gate",
                    self._response_count,
                )
                if response_id:
                    self._suppressed_response_ids.add(response_id)
                await self._send(oai, {"type": "response.cancel"})
                return
            logger.info("Bridge: response.created (#%d)", self._response_count)
            return

        if etype == "response.output_audio.delta":
            if event.get("response_id") in self._suppressed_response_ids:
                return  # cancelled noise response — never reach the caller
            chunk = base64.b64decode(event.get("delta", ""))
            if chunk:
                await self.audio.send_speaker_pcm16_24k(chunk)
            return

        if etype == "response.output_audio_transcript.delta":
            if event.get("response_id") in self._suppressed_response_ids:
                return
            delta = event.get("delta", "")
            if delta:
                self._current_agent += delta
                if self.emit:
                    await self.emit("transcript_delta", {"text": delta})
            return

        if etype == "response.done":
            logger.info("Bridge: response.done")
            self._response_in_flight = False
            response_id = (event.get("response") or {}).get("id") or ""
            was_suppressed = response_id in self._suppressed_response_ids
            self._suppressed_response_ids.discard(response_id)
            if was_suppressed:
                return  # noise-triggered turn: no audio, no transcript record
            self._conversational_response_done = True
            # Flush the accumulated assistant transcript as one labelled turn.
            if self._current_agent.strip():
                self._turns.append(f"Agent: {self._current_agent.strip()}")
            self._current_agent = ""
            await self._emit_turn()
            return

        if etype == "conversation.item.input_audio_transcription.completed":
            user_text = (event.get("transcript") or "").strip()
            if user_text:
                self._user_transcript_seen = True
                self._turns.append(f"User: {user_text}")
                if self.emit:
                    await self.emit("user_transcript", {"text": user_text})
                await self._emit_turn()
            return

        if etype == "conversation.item.input_audio_transcription.failed":
            logger.warning("Input transcription failed: %s", event.get("error"))
            return

        if etype == "input_audio_buffer.speech_started":
            self._speech_started_count += 1
            self._first_speech.set()
            logger.info("Bridge: speech_started (#%d) — VAD detected input",
                        self._speech_started_count)
            await self.audio.clear_playback()
            return

        if etype == "response.function_call_arguments.done":
            await self._handle_function_call(oai, event)
            return

        if etype == "error":
            err = event.get("error") or {}
            msg = err.get("message") or str(event)
            self._last_error = msg
            logger.warning("Realtime API error event: %s", msg)
            if self.emit:
                await self.emit("error", {"message": msg})
            return

    async def _handle_function_call(self, oai: websockets.WebSocketClientProtocol, event: dict) -> None:
        call_id = event.get("call_id", "")
        name = event.get("name", "")
        raw_args = event.get("arguments", "{}")
        try:
            args = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError:
            args = {}
            logger.warning("Realtime: malformed tool args for %s: %s", name, raw_args)

        tool = self._tool_handlers.get(name)
        if tool is None:
            output = json.dumps({"error": f"unknown tool: {name}"})
        else:
            try:
                result = await tool.handler(**args)
                if isinstance(result, ImageInjection):
                    output = result.text
                else:
                    output = result if isinstance(result, str) else json.dumps(result)
            except Exception as e:
                output = json.dumps({"error": str(e)})
                logger.warning("Realtime tool %s failed: %s", name, e, exc_info=True)

        self._tool_calls.append({"name": name, "arguments": args, "output": output[:500]})

        # ``end_call`` is a signal, not just a query: wind the session down.
        if name == "end_call":
            self._end_requested.set()
            return

        await self._send(oai, {
            "type": "conversation.item.create",
            "item": {"type": "function_call_output", "call_id": call_id, "output": output},
        })
        # Nudge the model to continue now that the tool result is in.
        await self._send(oai, {"type": "response.create"})

    async def _send(self, oai: websockets.WebSocketClientProtocol, payload: dict) -> None:
        await oai.send(json.dumps(payload))

    async def _emit_turn(self) -> None:
        """Fire on_turn with the current full transcript after a turn boundary.

        Best-effort: caller-level errors are logged but never break the bridge.
        """
        if not self.on_turn:
            return
        try:
            await self.on_turn("\n".join(self._turns).strip())
        except Exception:
            logger.warning("on_turn callback failed", exc_info=True)
