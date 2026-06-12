"""Ogg/Opus encoding and decoding for Twilio Media Streams.

Twilio's audio/x-opus-spogg format sends Ogg-wrapped Opus audio at 48kHz,
one Ogg page per media event. This module provides an incremental decoder
that accumulates pages and returns new PCM16 samples on each feed, plus
an encoder for outbound audio.
"""

from __future__ import annotations

import io
import logging
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

_OPUS_SAMPLE_RATE = 48000


def resample_48k_to_16k(pcm: np.ndarray) -> np.ndarray:
    """Downsample 48kHz PCM16 to 16kHz."""
    if len(pcm) == 0:
        return pcm
    return pcm[::3].copy()


def _resample(pcm: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    if len(pcm) == 0:
        return pcm
    if from_rate == to_rate:
        return pcm
    duration = len(pcm) / from_rate
    new_len = int(duration * to_rate)
    if new_len == 0:
        return np.array([], dtype=pcm.dtype)
    indices = np.linspace(0, len(pcm) - 1, new_len)
    return np.interp(indices, np.arange(len(pcm)), pcm).astype(pcm.dtype)


def split_ogg_pages(ogg_data: bytes) -> list[bytes]:
    """Split raw Ogg data into individual pages."""
    pages: list[bytes] = []
    i = 0
    while i < len(ogg_data):
        if ogg_data[i:i + 4] != b'OggS':
            break
        if i + 27 > len(ogg_data):
            break
        num_segments = ogg_data[i + 26]
        header_size = 27 + num_segments
        if i + header_size > len(ogg_data):
            break
        body_size = sum(ogg_data[i + 27:i + 27 + num_segments])
        page_size = header_size + body_size
        if i + page_size > len(ogg_data):
            break
        pages.append(ogg_data[i:i + page_size])
        i += page_size
    return pages


def encode_pcm_to_ogg_opus(pcm: np.ndarray, sample_rate: int) -> bytes:
    """Encode PCM16 audio to Ogg/Opus bytes for sending to Twilio."""
    import av

    if len(pcm) == 0:
        return b''

    pcm_48k = _resample(pcm, sample_rate, _OPUS_SAMPLE_RATE)
    audio_float = pcm_48k.astype(np.float32) / 32768.0
    audio_float = audio_float.reshape(1, -1)

    output = io.BytesIO()
    container = av.open(output, 'w', format='ogg')
    stream = container.add_stream('libopus', rate=_OPUS_SAMPLE_RATE)
    stream.layout = 'mono'

    frame = av.AudioFrame.from_ndarray(audio_float, format='flt', layout='mono')
    frame.sample_rate = _OPUS_SAMPLE_RATE

    for packet in stream.encode(frame):
        container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)

    container.close()
    return output.getvalue()


def _to_pcm16(samples: np.ndarray) -> np.ndarray:
    if np.issubdtype(samples.dtype, np.floating):
        return (samples * 32767).clip(-32768, 32767).astype(np.int16)
    return samples.astype(np.int16)


class OpusStreamDecoder:
    """Incremental Ogg/Opus decoder for Twilio Media Streams.

    Twilio sends one Ogg page per media event. The first two pages are
    Opus header pages (ID + comment); subsequent pages carry audio.
    """

    def __init__(self) -> None:
        self._header_buf = bytearray()
        self._header_complete = False
        self._accumulated: list[np.ndarray] = []

    def feed(self, ogg_page: bytes) -> np.ndarray | None:
        """Feed one Ogg page. Returns any new PCM16 (48kHz), or None."""
        if not self._header_complete:
            logger.debug("OPUS feed: header phase, buf=%d bytes, page=%d bytes", len(self._header_buf), len(ogg_page))
            self._header_buf.extend(ogg_page)
            pcm = self._try_decode(bytes(self._header_buf))
            if pcm is not None and len(pcm) > 0:
                self._header_complete = True
                # Keep only the header pages for future decoding
                pages = split_ogg_pages(bytes(self._header_buf))
                self._header_buf = bytearray(b''.join(pages[:2]))
                self._accumulated.append(pcm)
                return pcm
            return None

        # Decode audio page with cached headers
        combined = bytes(self._header_buf) + ogg_page
        pcm = self._try_decode(combined)
        if pcm is not None and len(pcm) > 0:
            self._accumulated.append(pcm)
            return pcm
        return None

    def _try_decode(self, data: bytes) -> np.ndarray | None:
        if not data:
            return None
        try:
            import av
            container = av.open(io.BytesIO(data))
            frames = []
            for frame in container.decode(audio=0):
                frames.append(frame.to_ndarray().flatten())
            container.close()
            if not frames:
                return None
            return _to_pcm16(np.concatenate(frames))
        except Exception as e:
            logger.warning("OPUS decode failed for %d bytes: %s", len(data), e)
            return None

    def get_all_pcm16(self) -> np.ndarray:
        """Return all accumulated PCM16 at 48kHz and reset."""
        if not self._accumulated:
            return np.array([], dtype=np.int16)
        pcm = np.concatenate(self._accumulated)
        self._accumulated.clear()
        return pcm

    def clear(self) -> None:
        """Reset accumulated audio but preserve stream headers."""
        self._accumulated.clear()
        self._accumulated.clear()
        self._buf.clear()
        self._header_buf.clear()
        self._header_complete = False
