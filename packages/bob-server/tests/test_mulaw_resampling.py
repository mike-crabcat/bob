"""Tests for mulaw resampling — anti-aliased downsampling, gain, streaming state."""

from __future__ import annotations

import numpy as np

from bob_server.services.mulaw import (
    AntiAliasedDownsampler,
    apply_gain,
    resample_24k_to_8k,
)


def _sine(freq_hz: float, sample_rate: float, seconds: float = 1.0) -> np.ndarray:
    t = np.arange(int(seconds * sample_rate)) / sample_rate
    return (0.5 * 32767 * np.sin(2 * np.pi * freq_hz * t)).astype(np.int16)


def _rms(pcm: np.ndarray) -> float:
    if len(pcm) == 0:
        return 0.0
    f = pcm.astype(np.float64)
    return float(np.sqrt(np.mean(f * f)))


def test_downsample_suppresses_above_nyquist_6khz_tone():
    """A 6 kHz tone at 24k MUST be filtered out, not aliased to 2 kHz.

    The old pcm[::3] implementation passed it through at full amplitude.
    """
    out = resample_24k_to_8k(_sine(6000, 24000))
    # ignore filter edge samples
    core = out[200:-200]
    assert _rms(core) < 0.02 * _rms(_sine(6000, 24000)), (
        f"6kHz tone leaked through: rms={_rms(core)}"
    )


def test_downsample_passes_speech_band_1khz_tone():
    out = resample_24k_to_8k(_sine(1000, 24000))
    core = out[200:-200]
    assert _rms(core) > 0.5 * _rms(_sine(1000, 24000)), (
        f"in-band 1kHz tone attenuated too much: rms={_rms(core)}"
    )


def test_stateful_downsampler_length_accounting():
    ds = AntiAliasedDownsampler(factor=3, sample_rate=24000.0)
    total_in, total_out = 0, 0
    for chunk_len in (1000, 500, 7, 2400, 1):  # awkward sizes on purpose
        out = ds.process(np.zeros(chunk_len, dtype=np.int16))
        total_in += chunk_len
        total_out += len(out)
    # One output per 3 absolute input samples: ceil(total_in / 3)
    assert total_out == (total_in + 2) // 3


def test_stateful_downsampler_suppresses_alias_across_chunks():
    ds = AntiAliasedDownsampler(factor=3, sample_rate=24000.0)
    tone = _sine(6000, 24000)
    outs = [ds.process(tone[i:i + 1000]) for i in range(0, len(tone), 1000)]
    out = np.concatenate(outs)
    core = out[200:-200]
    assert _rms(core) < 0.02 * _rms(tone), f"aliased tone across chunks: rms={_rms(core)}"


def test_apply_gain_boosts_and_clips():
    quiet = np.array([100, -100, 5000, -5000], dtype=np.int16)
    boosted = apply_gain(quiet, 4.0)
    assert boosted[0] == 400
    assert boosted[2] == 20000
    loud = np.array([20000, -20000], dtype=np.int16)
    clipped = apply_gain(loud, 4.0)
    assert clipped[0] == 32767  # clip-protected, no int16 wraparound
    assert clipped[1] == -32768


def test_apply_gain_identity():
    pcm = np.array([1, -2, 3], dtype=np.int16)
    out = apply_gain(pcm, 1.0)
    assert np.array_equal(out, pcm)


def _active_spans(ch: np.ndarray, sr: int = 24000) -> list[tuple[float, float]]:
    active = np.abs(ch) > 0.05
    if not active.any():
        return []
    edges = np.diff(active.astype(int))
    starts = list(np.where(edges == 1)[0] + 1)
    ends = list(np.where(edges == -1)[0] + 1)
    if active[0]:
        starts.insert(0, 0)
    if active[-1]:
        ends.append(len(active))
    return [(s / sr, e / sr) for s, e in zip(starts, ends)]


def test_recording_channels_are_time_aligned(tmp_path):
    """Bob's audio must appear at its wall-clock position, not packed at t=0."""
    from bob_server.services.realtime_bridge import TwilioMediaSource

    src = TwilioMediaSource(ws=None, stream_sid="test")
    src._start_monotonic = 1000.0  # pretend the call started 1000s ago

    # User speaks at t=1.0s for 0.5s (24k PCM16 = 12000 samples)
    user_chunk = (np.ones(12000, dtype=np.int16) * 8000).tobytes()
    src.rec_inbound_pcm24.append((1.0, user_chunk))
    # Bob speaks at t=2.0s for 0.5s
    bob_chunk = (np.ones(12000, dtype=np.int16) * 8000).tobytes()
    src.rec_outbound_pcm24.append((2.0, bob_chunk))

    result = src.finalize_recording(tmp_path, "rectest")
    assert result is not None
    import soundfile as sf
    data, sr = sf.read(tmp_path / "rectest.wav")
    assert sr == 24000
    assert len(data) / sr >= 2.5  # covers Bob's speech ending at 2.5s

    left_spans = _active_spans(data[:, 0])
    right_spans = _active_spans(data[:, 1])
    assert len(left_spans) == 1 and abs(left_spans[0][0] - 1.0) < 0.05, left_spans
    assert len(right_spans) == 1 and abs(right_spans[0][0] - 2.0) < 0.05, right_spans


def test_recording_sequential_outbound_does_not_collapse_gaps(tmp_path):
    """Two Bob utterances with a pause between must keep that pause."""
    from bob_server.services.realtime_bridge import TwilioMediaSource

    src = TwilioMediaSource(ws=None, stream_sid="test")
    src._start_monotonic = 1000.0
    chunk = (np.ones(4800, dtype=np.int16) * 8000).tobytes()  # 0.2s
    src.rec_outbound_pcm24.append((1.0, chunk))
    src.rec_outbound_pcm24.append((4.0, chunk))  # 2.8s pause after first

    assert src.finalize_recording(tmp_path, "rectest2") is not None
    import soundfile as sf
    data, sr = sf.read(tmp_path / "rectest2.wav")
    spans = _active_spans(data[:, 1])
    assert len(spans) == 2, f"pause collapsed: {spans}"
    gap = spans[1][0] - spans[0][1]
    assert gap > 2.0, f"expected ~2.8s gap, got {gap:.2f}s"
