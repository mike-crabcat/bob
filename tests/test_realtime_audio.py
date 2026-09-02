"""Audio-format round-trip tests for the Realtime phone path.

The Twilio path converts μ-law 8kHz ↔ PCM16 24kHz. If the resampling or
encoding is broken, a live phone call burns money on garbage audio. These
tests cover the conversions services/realtime_bridge.py relies on.
"""

from __future__ import annotations

import numpy as np

from server.services.mulaw import (
    mulaw_to_pcm16,
    pcm16_to_mulaw,
    resample_24k_to_8k,
    resample_8k_to_24k,
)


def _sine(freq_hz: float, sample_rate: int, duration_s: float) -> np.ndarray:
    t = np.arange(int(sample_rate * duration_s)) / sample_rate
    wave = np.sin(2 * np.pi * freq_hz * t)
    return (wave * 32767 * 0.8).astype(np.int16)


def test_resample_8k_to_24k_triples_length():
    pcm8k = np.zeros(8000, dtype=np.int16)  # 1 second of silence
    pcm24k = resample_8k_to_24k(pcm8k)
    assert len(pcm24k) == 24000
    assert pcm24k.dtype == np.int16


def test_resample_24k_to_8k_thirds_length():
    pcm24k = np.zeros(24000, dtype=np.int16)
    pcm8k = resample_24k_to_8k(pcm24k)
    assert len(pcm8k) == 8000
    assert pcm8k.dtype == np.int16


def test_mulaw_round_trip_preserves_shape():
    """μ-law is lossy but a pure tone should survive encode → decode with high correlation."""
    original = _sine(440, 8000, 0.5)
    mulaw = pcm16_to_mulaw(original)
    decoded = mulaw_to_pcm16(mulaw)
    assert len(decoded) == len(original)
    # Correlation between original and decoded; μ-law quantisation keeps a pure tone recognisable.
    corr = np.corrcoef(original.astype(np.float64), decoded.astype(np.float64))[0, 1]
    assert corr > 0.95


def test_full_phone_path_round_trip():
    """The exact path TwilioMediaSource uses: 8k μ-law → 24k PCM → back to 8k μ-law.

    A 440Hz tone should remain highly correlated with the original after the full loop.
    This is the conversion chain that would carry a live phone call's audio.
    """
    original_pcm8k = _sine(440, 8000, 0.5)
    mulaw_in = pcm16_to_mulaw(original_pcm8k)

    # Inbound: μ-law → PCM16 8k → upsample to 24k (sent to OpenAI)
    pcm8k_decoded = mulaw_to_pcm16(mulaw_in)
    pcm24k = resample_8k_to_24k(pcm8k_decoded)

    # Outbound: PCM16 24k (from OpenAI) → downsample to 8k → μ-law (sent to Twilio)
    pcm8k_back = resample_24k_to_8k(pcm24k)
    mulaw_out = pcm16_to_mulaw(pcm8k_back)
    final_pcm8k = mulaw_to_pcm16(mulaw_out)

    assert len(final_pcm8k) == len(original_pcm8k)
    corr = np.corrcoef(original_pcm8k.astype(np.float64), final_pcm8k.astype(np.float64))[0, 1]
    # Two passes of μ-law plus resampling degrade the signal but a pure tone stays recognisable.
    assert corr > 0.90, f"round-trip correlation too low: {corr:.3f}"


def test_empty_resamples_are_safe():
    """Edge case: empty buffers must not raise (Twilio may send tiny/empty frames)."""
    empty = np.array([], dtype=np.int16)
    assert len(resample_8k_to_24k(empty)) == 0
    assert len(resample_24k_to_8k(empty)) == 0
