"""Audio transport abstraction for the voice pipeline.

Provides a unified interface so VoiceService can work with any audio
source/sink: browser WebSockets, Twilio Media Streams, etc.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import time
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from fastapi import WebSocket

from cyborg_server.services.mulaw import (
    mulaw_to_pcm16,
    pcm16_to_mulaw,
    resample_24k_to_8k,
    rms_energy,
    wav_bytes_to_pcm16,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class VoiceTransport(Protocol):
    """Abstract audio I/O for the voice pipeline."""

    async def send_audio(self, wav_bytes: bytes) -> None:
        """Send a WAV audio chunk (TTS output) to the client/caller."""
        ...

    async def send_status(self, state: str) -> None:
        """Send a status update (e.g. 'recording', 'thinking', 'speaking')."""
        ...

    async def send_message(self, msg_type: str, data: dict) -> None:
        """Send a structured JSON message."""
        ...

    async def send_error(self, message: str) -> None:
        """Send an error message."""
        ...


class BrowserTransport:
    """Transport for browser-based voice chat over WebSocket.

    Wraps the existing behavior: binary WAV frames for audio,
    JSON text messages for status/events.
    """

    def __init__(self, websocket: WebSocket) -> None:
        self._ws = websocket

    async def send_audio(self, wav_bytes: bytes) -> None:
        try:
            await self._ws.send_bytes(wav_bytes)
        except Exception:
            pass

    async def send_status(self, state: str) -> None:
        try:
            await self._ws.send_text(json.dumps({"type": "status", "state": state}))
        except Exception:
            pass

    async def send_message(self, msg_type: str, data: dict) -> None:
        try:
            payload = {"type": msg_type, **data}
            await self._ws.send_text(json.dumps(payload))
        except Exception:
            pass

    async def send_error(self, message: str) -> None:
        try:
            await self._ws.send_text(json.dumps({"type": "error", "message": message}))
        except Exception:
            pass


class TwilioTransport:
    """Transport for Twilio Media Stream WebSocket.

    Handles μ-law encoding/decoding and the Twilio Media Stream message format.
    """

    def __init__(
        self,
        websocket: WebSocket,
        stream_sid: str,
        silence_threshold: float = 0.01,
        silence_duration: float = 1.5,
        record: bool = False,
    ) -> None:
        self._ws = websocket
        self._stream_sid = stream_sid
        self._silence_threshold = silence_threshold
        self._silence_duration = silence_duration
        self._audio_buffer: list[bytes] = []
        self._last_speech_time: float = 0.0
        self._has_speech: bool = False
        self._send_lock = asyncio.Lock()
        self._interrupted: bool = False
        self._record = record
        self._rec_start: float = 0.0
        self._rec_inbound: list[tuple[float, Any]] = []   # (timestamp, pcm_8k)
        self._rec_outbound: list[tuple[float, Any, int]] = []   # (timestamp, pcm, sr)

    @property
    def stream_sid(self) -> str:
        return self._stream_sid

    @property
    def is_speaking(self) -> bool:
        return self._send_lock.locked()

    def interrupt(self) -> None:
        self._interrupted = True

    def reset_interrupt(self) -> None:
        self._interrupted = False

    async def send_audio(self, wav_bytes: bytes) -> None:
        """Convert WAV to μ-law and send to Twilio."""
        async with self._send_lock:
            try:
                pcm, sr = wav_bytes_to_pcm16(wav_bytes)
                if self._record:
                    self._rec_outbound.append((time.monotonic(), pcm.copy(), sr))
                logger.info("TwilioTransport.send_audio: %d samples at %dHz", len(pcm), sr)
                # Resample to 8kHz
                if sr == 24000:
                    pcm_8k = resample_24k_to_8k(pcm)
                elif sr == 16000:
                    from cyborg_server.services.mulaw import resample_16k_to_8k
                    pcm_8k = resample_16k_to_8k(pcm)
                elif sr == 8000:
                    pcm_8k = pcm
                else:
                    # Generic resample via numpy
                    import numpy as np
                    duration = len(pcm) / sr
                    new_len = int(duration * 8000)
                    old_indices = np.linspace(0, len(pcm) - 1, new_len)
                    pcm_8k = np.interp(old_indices, np.arange(len(pcm)), pcm).astype(np.int16)

                mulaw = pcm16_to_mulaw(pcm_8k)
                logger.info("TwilioTransport.send_audio: %d bytes μ-law, sending in chunks", len(mulaw))

                # Twilio expects 20ms chunks (160 bytes of μ-law at 8kHz)
                chunk_size = 160
                chunks_sent = 0
                for i in range(0, len(mulaw), chunk_size):
                    if self._interrupted:
                        logger.info("TwilioTransport: speech interrupted after %d chunks", chunks_sent)
                        return
                    chunk_mulaw = mulaw[i : i + chunk_size]
                    chunk_payload = base64.b64encode(chunk_mulaw).decode("ascii")
                    msg = json.dumps({
                        "event": "media",
                        "streamSid": self._stream_sid,
                        "media": {"payload": chunk_payload},
                    })
                    try:
                        await self._ws.send_text(msg)
                        chunks_sent += 1
                    except Exception as e:
                        logger.warning("TwilioTransport.send_audio: send failed at chunk %d: %s", chunks_sent, e)
                        return

                    # Pace to real-time: 20ms per 160-byte chunk
                    await asyncio.sleep(0.02)

                logger.info("TwilioTransport.send_audio: sent %d chunks OK", chunks_sent)

            except Exception:
                logger.warning("Failed to send audio via Twilio transport", exc_info=True)

    async def send_status(self, state: str) -> None:
        # Phone has no status UI — no-op
        pass

    async def send_message(self, msg_type: str, data: dict) -> None:
        # Phone has no text message display — no-op
        pass

    async def send_error(self, message: str) -> None:
        logger.warning("Phone error: %s", message)

    def feed_inbound_audio(self, mulaw_bytes: bytes) -> None:
        """Feed inbound μ-law audio from Twilio into the silence detector."""
        pcm = mulaw_to_pcm16(mulaw_bytes)
        if self._record:
            now = time.monotonic()
            if not self._rec_start:
                self._rec_start = now
            self._rec_inbound.append((now, pcm.copy()))
        energy = rms_energy(pcm)
        now = time.monotonic()

        if energy > self._silence_threshold:
            self._last_speech_time = now
            self._has_speech = True

        # Always buffer audio while speech or within silence window
        self._audio_buffer.append(mulaw_bytes)

    def is_utterance_complete(self) -> bool:
        """Check if the caller has stopped speaking."""
        if not self._has_speech:
            return False
        now = time.monotonic()
        return (now - self._last_speech_time) >= self._silence_duration

    def get_accumulated_pcm16(self) -> Any:
        """Get the buffered audio as 16kHz PCM16 and clear the buffer."""
        import numpy as np
        if not self._audio_buffer:
            return np.array([], dtype=np.int16)
        raw = b"".join(self._audio_buffer)
        self._audio_buffer.clear()
        self._has_speech = False
        pcm_8k = mulaw_to_pcm16(raw)
        from cyborg_server.services.mulaw import resample_8k_to_16k
        return resample_8k_to_16k(pcm_8k)

    def clear_buffer(self) -> None:
        """Discard buffered audio."""
        self._audio_buffer.clear()
        self._has_speech = False

    def finalize_recording(self, calls_dir: Path, call_id: str) -> tuple[str, int] | None:
        """Write time-aligned stereo WAV and return (relative_path, size_bytes)."""
        if not self._record or (not self._rec_inbound and not self._rec_outbound):
            return None

        import numpy as np
        import soundfile as sf

        target_sr = 16000

        # Determine timeline start and end
        all_times = [t for t, _ in self._rec_inbound] + [t for t, _, _ in self._rec_outbound]
        start = self._rec_start or min(all_times)
        end = max(all_times)

        # Add a small tail so the last chunk isn't cut off
        if self._rec_outbound:
            last_out_time = max(t for t, _, _ in self._rec_outbound)
            last_pcm, last_sr = self._rec_outbound[-1][1], self._rec_outbound[-1][2]
            last_dur = len(last_pcm) / last_sr
            end = max(end, last_out_time + last_dur)
        if self._rec_inbound:
            end += 0.02  # 20ms tail for last inbound chunk

        total_samples = int((end - start) * target_sr)
        if total_samples <= 0:
            return None

        left = np.zeros(total_samples, dtype=np.float32)
        right = np.zeros(total_samples, dtype=np.float32)

        # Place inbound chunks on left channel (caller)
        from cyborg_server.services.mulaw import resample_8k_to_16k
        for ts, pcm_8k in self._rec_inbound:
            offset = int((ts - start) * target_sr)
            pcm_16k = resample_8k_to_16k(pcm_8k).astype(np.float32) / 32768.0
            end_idx = min(offset + len(pcm_16k), total_samples)
            if offset < total_samples and end_idx > offset:
                left[offset:end_idx] = pcm_16k[:end_idx - offset]

        # Place outbound chunks on right channel (assistant)
        def _resample_to_16k(pcm: Any, sr: int) -> Any:
            if sr == target_sr:
                return pcm
            if sr == 8000:
                return resample_8k_to_16k(pcm)
            duration = len(pcm) / sr
            new_len = int(duration * target_sr)
            if new_len == 0:
                return np.array([], dtype=np.int16)
            indices = np.linspace(0, len(pcm) - 1, new_len)
            return np.interp(indices, np.arange(len(pcm)), pcm).astype(np.int16)

        for ts, pcm, sr in self._rec_outbound:
            offset = int((ts - start) * target_sr)
            pcm_16k = _resample_to_16k(pcm, sr).astype(np.float32) / 32768.0
            end_idx = min(offset + len(pcm_16k), total_samples)
            if offset < total_samples and end_idx > offset:
                right[offset:end_idx] = pcm_16k[:end_idx - offset]

        stereo = np.column_stack([left, right])
        calls_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{call_id}.wav"
        path = calls_dir / filename
        sf.write(str(path), stereo, target_sr, subtype="PCM_16", format="WAV")

        self._rec_inbound.clear()
        self._rec_outbound.clear()
        return (filename, path.stat().st_size)
