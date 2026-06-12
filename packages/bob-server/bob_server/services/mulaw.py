"""μ-law (G.711) encoding/decoding and sample-rate conversion utilities.

Twilio Media Streams use 8 kHz μ-law audio. Our STT expects 16 kHz PCM16
and our TTS outputs 24 kHz float32. These helpers bridge the gap.
"""

from __future__ import annotations

from typing import Any

# Lazy numpy — loaded on first use.
_np: Any = None


def _ensure_numpy() -> Any:
    global _np
    if _np is None:
        import numpy
        _np = numpy
    return _np


# Build μ-law decode table (standard G.711).
def _build_mulaw_decode_table() -> list[int]:
    table = [0] * 256
    for i in range(256):
        val = i ^ 0xFF
        sign = 1 if (val & 0x80) == 0 else -1
        exponent = (val >> 4) & 0x07
        mantissa = val & 0x0F
        decoded = (mantissa * 2 + 33) * (1 << exponent) - 33
        table[i] = sign * decoded
    return table


_DECODE_TABLE = _build_mulaw_decode_table()


# Build μ-law encode table as the inverse of the decode table.
# For each int16 value, find the μ-law byte whose decoded value is closest.
def _build_mulaw_encode_table() -> list[int]:
    pairs = sorted(
        [(_DECODE_TABLE[b], b) for b in range(256)],
        key=lambda x: x[0],
    )
    table = [0] * 65536
    idx = 0
    for pcm in range(-32768, 32768):
        while idx < len(pairs) - 1 and abs(pairs[idx + 1][0] - pcm) <= abs(pairs[idx][0] - pcm):
            idx += 1
        table[pcm + 32768] = pairs[idx][1]
    return table


_ENCODE_TABLE = _build_mulaw_encode_table()


def mulaw_to_pcm16(mulaw_bytes: bytes) -> Any:
    """Decode μ-law bytes to int16 numpy array."""
    np = _ensure_numpy()
    arr = np.frombuffer(mulaw_bytes, dtype=np.uint8)
    table = np.array(_DECODE_TABLE, dtype=np.int16)
    return table[arr].copy()


def pcm16_to_mulaw(pcm: Any) -> bytes:
    """Encode int16 numpy array to μ-law bytes."""
    np = _ensure_numpy()
    pcm = np.clip(pcm, -32768, 32767).astype(np.int16)
    indices = pcm.astype(np.int16).astype(np.int32) + 32768
    table = np.array(_ENCODE_TABLE, dtype=np.uint8)
    return table[indices].tobytes()


def resample_8k_to_16k(pcm: Any) -> Any:
    """Upsample 8 kHz PCM16 to 16 kHz by linear interpolation."""
    np = _ensure_numpy()
    if len(pcm) == 0:
        return pcm
    duration = len(pcm) / 8000.0
    new_len = int(duration * 16000)
    if new_len == 0:
        return np.array([], dtype=pcm.dtype)
    old_indices = np.linspace(0, len(pcm) - 1, new_len)
    return np.interp(old_indices, np.arange(len(pcm)), pcm).astype(pcm.dtype)


def resample_24k_to_8k(pcm: Any) -> Any:
    """Downsample 24 kHz PCM16 to 8 kHz."""
    if len(pcm) == 0:
        return pcm
    return pcm[::3].copy()


def resample_16k_to_8k(pcm: Any) -> Any:
    """Downsample 16 kHz PCM16 to 8 kHz."""
    if len(pcm) == 0:
        return pcm
    return pcm[::2].copy()


def wav_bytes_to_pcm16(wav_bytes: bytes) -> tuple[Any, int]:
    """Parse WAV bytes into (pcm_int16_array, sample_rate)."""
    import io
    import soundfile as sf

    data, sr = sf.read(io.BytesIO(wav_bytes), dtype="int16")
    return data, sr


def pcm16_to_wav_bytes(pcm: Any, sample_rate: int) -> bytes:
    """Convert int16 PCM array to WAV bytes."""
    import io
    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, pcm, sample_rate, subtype="PCM_16", format="WAV")
    return buf.getvalue()


def rms_energy(pcm: Any) -> float:
    """Compute RMS energy of a PCM signal (any int or float type)."""
    np = _ensure_numpy()
    if len(pcm) == 0:
        return 0.0
    f = pcm.astype(np.float64)
    return float(np.sqrt(np.mean(f * f)))
