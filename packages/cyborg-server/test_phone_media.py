"""Test script that simulates a Twilio Media Stream client.

Connects to the local /phone/media WebSocket, sends audio through
the voice pipeline, and validates the round-trip.

Usage:
    cd packages/cyborg-server
    .venv/bin/python ../../test_phone_media.py [path/to/test.wav]

If no WAV file is given, generates a 2-second test tone.
"""

import asyncio
import base64
import io
import json
import sys
import time
import wave

try:
    import numpy as np
    import soundfile as sf
    import websockets
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Install with: pip install numpy soundfile websockets")
    sys.exit(1)

from cyborg_server.services.mulaw import pcm16_to_mulaw, resample_24k_to_8k, resample_16k_to_8k

WS_URL = "ws://localhost:8420/phone/media"
STREAM_SID = "test-stream-001"


def generate_test_tone(frequency: float = 440.0, duration: float = 2.0, sr: int = 16000) -> bytes:
    """Generate a simple sine wave WAV as if it were speech audio."""
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    samples = (0.5 * np.sin(2 * np.pi * frequency * t) * 32767).astype(np.int16)
    buf = io.BytesIO()
    sf.write(buf, samples, sr, subtype="PCM_16", format="WAV")
    return buf.getvalue()


def load_wav(path: str) -> tuple[bytes, int]:
    """Load a WAV file and return (wav_bytes, sample_rate)."""
    data, sr = sf.read(path, dtype="int16")
    buf = io.BytesIO()
    sf.write(buf, data, sr, subtype="PCM_16", format="WAV")
    return buf.getvalue(), sr


def wav_to_mulaw_chunks(wav_bytes: bytes, chunk_ms: int = 20) -> list[bytes]:
    """Convert WAV bytes to a list of μ-law chunks (20ms each at 8kHz)."""
    data, sr = sf.read(io.BytesIO(wav_bytes), dtype="int16")
    # Downsample to 8kHz
    if sr == 24000:
        pcm_8k = resample_24k_to_8k(data)
    elif sr == 16000:
        pcm_8k = resample_16k_to_8k(data)
    elif sr == 8000:
        pcm_8k = data
    else:
        duration = len(data) / sr
        new_len = int(duration * 8000)
        old_indices = np.linspace(0, len(data) - 1, new_len)
        pcm_8k = np.interp(old_indices, np.arange(len(data)), data).astype(np.int16)

    mulaw = pcm16_to_mulaw(pcm_8k)
    chunk_size = 160  # 20ms at 8kHz = 160 bytes
    chunks = [mulaw[i:i + chunk_size] for i in range(0, len(mulaw), chunk_size)]
    return chunks


def generate_silence_chunks(duration: float = 3.0) -> list[bytes]:
    """Generate silence μ-law chunks to trigger end-of-utterance."""
    num_samples = int(8000 * duration)
    silence = np.zeros(num_samples, dtype=np.int16)
    mulaw = pcm16_to_mulaw(silence)
    chunk_size = 160
    return [mulaw[i:i + chunk_size] for i in range(0, len(mulaw), chunk_size)]


async def run_test(wav_path: str | None = None):
    print(f"Connecting to {WS_URL}...")

    async with websockets.connect(WS_URL) as ws:
        print("Connected!")

        # Send Twilio 'connected' event
        await ws.send(json.dumps({
            "event": "connected",
            "protocol": "Call",
        }))
        print("Sent 'connected' event")

        # Send Twilio 'start' event
        await ws.send(json.dumps({
            "event": "start",
            "streamSid": STREAM_SID,
            "start": {
                "streamSid": STREAM_SID,
                "accountSid": "test",
                "callSid": "test-call-001",
            },
        }))
        print(f"Sent 'start' event (streamSid={STREAM_SID})")

        # Load or generate audio
        if wav_path:
            print(f"Loading {wav_path}...")
            wav_bytes, sr = load_wav(wav_path)
        else:
            print("Generating 2-second test tone (440Hz)...")
            wav_bytes = generate_test_tone()
            sr = 16000

        # Convert to μ-law chunks
        chunks = wav_to_mulaw_chunks(wav_bytes)
        print(f"Audio: {len(chunks)} chunks ({len(chunks) * 20}ms)")

        # Send audio chunks
        t_start = time.monotonic()
        for i, chunk in enumerate(chunks):
            payload = base64.b64encode(chunk).decode("ascii")
            await ws.send(json.dumps({
                "event": "media",
                "streamSid": STREAM_SID,
                "media": {
                    "track": "inbound",
                    "chunk": i + 1,
                    "payload": payload,
                },
            }))
            # Pace to real-time (20ms per chunk)
            await asyncio.sleep(0.02)

        audio_sent_at = time.monotonic()
        print(f"Sent all audio in {audio_sent_at - t_start:.1f}s")

        # Send silence to trigger end-of-utterance detection
        silence_chunks = generate_silence_chunks(duration=3.0)
        print(f"Sending {len(silence_chunks)} silence chunks to trigger utterance end...")
        for chunk in silence_chunks:
            payload = base64.b64encode(chunk).decode("ascii")
            await ws.send(json.dumps({
                "event": "media",
                "streamSid": STREAM_SID,
                "media": {"payload": payload},
            }))
            await asyncio.sleep(0.02)

        # Collect response audio
        print("Waiting for response audio...")
        response_chunks = 0
        response_bytes = 0
        timeout = 60  # Wait up to 60s for the full pipeline

        try:
            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except asyncio.TimeoutError:
                    print(f"Timeout after {timeout}s")
                    break

                if isinstance(msg, bytes):
                    # Shouldn't get binary — Twilio protocol is all JSON
                    print(f"  [unexpected binary: {len(msg)} bytes]")
                    continue

                data = json.loads(msg)
                event = data.get("event")

                if event == "media":
                    payload = data.get("media", {}).get("payload", "")
                    chunk_bytes = base64.b64decode(payload) if payload else b""
                    response_chunks += 1
                    response_bytes += len(chunk_bytes)
                    if response_chunks <= 3:
                        print(f"  Audio chunk {response_chunks}: {len(chunk_bytes)} bytes")
                elif event == "stop":
                    print("  Stream stopped by server")
                    break
                else:
                    print(f"  Event: {event}")

        except websockets.ConnectionClosed:
            print("WebSocket closed")

        elapsed = time.monotonic() - audio_sent_at
        print(f"\n--- Results ---")
        print(f"Response chunks: {response_chunks}")
        print(f"Response bytes:  {response_bytes}")
        print(f"Latency:         {elapsed:.1f}s")

        if response_chunks > 0:
            print("\nSUCCESS: Voice pipeline responded with audio!")
        else:
            print("\nFAILED: No audio response received")

        # Send stop event
        try:
            await ws.send(json.dumps({
                "event": "stop",
                "streamSid": STREAM_SID,
            }))
        except Exception:
            pass


if __name__ == "__main__":
    wav_path = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(run_test(wav_path))
