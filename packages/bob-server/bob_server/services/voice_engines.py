"""STT and TTS engine singletons for the voice chat subsystem.

GPU models are loaded once at startup and shared across all WebSocket connections.
All heavy imports are deferred to avoid pulling in torch/whisper/omnivoice when
voice is disabled.
"""

from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path
from typing import Any

from bob_server.config import VoiceSettings

logger = logging.getLogger(__name__)


def _convert_to_wav(audio_data: bytes) -> bytes:
    """Convert arbitrary audio bytes to 16kHz mono WAV using PyAV."""
    import av

    inp = av.open(io.BytesIO(audio_data))
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    out_buf = io.BytesIO()
    out = av.open(out_buf, mode="w", format="wav")
    out_stream = out.add_stream("pcm_s16le", rate=16000, layout="mono")

    for frame in inp.decode(audio=0):  # type: ignore[union-attr]
        for rf in resampler.resample(frame):
            for packet in out_stream.encode(rf):
                out.mux(packet)

    for packet in out_stream.encode(None):
        out.mux(packet)

    out.close()
    inp.close()
    return out_buf.getvalue()


def samples_to_wav(samples: Any, sample_rate: int) -> bytes:
    """Convert numpy float32 samples to WAV bytes."""
    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, samples, sample_rate, subtype="PCM_16", format="WAV")
    return buf.getvalue()


def generate_tone_wav(
    frequency: float = 440.0,
    duration: float = 0.3,
    sample_rate: int = 16000,
    amplitude: float = 0.3,
) -> bytes:
    import numpy as np

    t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)
    samples = (amplitude * np.sin(2 * np.pi * frequency * t)).astype(np.float32)
    buf = io.BytesIO()
    import soundfile as sf

    sf.write(buf, samples, sample_rate, subtype="PCM_16", format="WAV")
    return buf.getvalue()


class STTEngine:
    """Speech-to-text using faster-whisper (GPU-accelerated)."""

    def __init__(self, model_size: str = "large-v3-turbo", device: str = "cuda", compute_type: str = "int8") -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._model: Any = None

    def preload(self) -> None:
        logger.info("Preloading STT model (%s, %s)...", self._model_size, self._device)
        self._ensure_model()
        logger.info("STT model ready")

    def _ensure_model(self) -> Any:
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self._model_size, device=self._device, compute_type=self._compute_type,
            )
        return self._model

    def transcribe(self, audio_data: bytes, language: str | None = None) -> tuple[str, str]:
        """Transcribe audio data. Returns (text, detected_language)."""
        model = self._ensure_model()

        try:
            wav_bytes = _convert_to_wav(audio_data)
        except Exception:
            logger.error("Audio conversion failed", exc_info=True)
            return "", language or "en"

        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav_bytes)
            wav_path = f.name

        try:
            segments, info = model.transcribe(wav_path, language=language)
            text = " ".join(seg.text.strip() for seg in segments).strip()
            return text, info.language
        finally:
            Path(wav_path).unlink(missing_ok=True)


_VOICE_PROFILES: dict[str, dict[str, Any]] = {
    "en": {
        "instruct": "male, australian accent, young adult",
        "ref_text": "G'day! I'm Bob, your friendly voice assistant. I'm here to help you learn and practice all sorts of new things.",
        "speed": 1.0,
    },
    "fr": {
        "instruct": "female, young adult",
        "ref_text": "Bonjour! Je suis votre professeur de français. Je suis ravie de vous aider à apprendre cette belle langue.",
        "speed": 1.0,
    },
    "pt": {
        "instruct": "female, portuguese accent, young adult",
        "ref_text": "Olá! Eu sou sua professora de português brasileiro. É um prazer ajudar você a aprender esta língua maravilhosa.",
        "speed": 1.0,
    },
}

_DEFAULT_VOICE = "en"


class TTSEngine:
    """Text-to-speech using OmniVoice with voice cloning."""

    def __init__(self, num_steps: int = 32) -> None:
        self._num_steps = num_steps
        self._model: Any = None
        self._voice_prompts: dict[str, Any] = {}
        self.lock = asyncio.Lock()

    def preload(self, voices_dir: Path) -> None:
        self._ensure_model()
        for lang in _VOICE_PROFILES:
            wav_path = self._ensure_ref_audio(voices_dir, lang)
            logger.info("Creating voice clone prompt for %s from %s", lang, wav_path)
            self._voice_prompts[lang] = self._model.create_voice_clone_prompt(str(wav_path))
        logger.info("All voices ready: %s", list(self._voice_prompts.keys()))

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        from omnivoice import OmniVoice

        logger.info("Loading OmniVoice model (first call)...")
        self._model = OmniVoice.from_pretrained(
            "k2-fsa/OmniVoice", device_map="cuda:0", dtype="float16",
        )
        logger.info("OmniVoice model loaded")

    def _ensure_ref_audio(self, voices_dir: Path, lang: str) -> Path:
        wav_path = voices_dir / f"{lang}.wav"
        if wav_path.exists():
            return wav_path

        profile = _VOICE_PROFILES[lang]
        logger.info("Generating reference audio for %s voice: %s", lang, profile["instruct"])

        from omnivoice import OmniVoiceGenerationConfig

        audio_arrays = self._model.generate(
            text=profile["ref_text"],
            language=lang,
            instruct=profile["instruct"],
            generation_config=OmniVoiceGenerationConfig(num_step=self._num_steps),
        )
        audio = audio_arrays[0]

        wav_path.parent.mkdir(parents=True, exist_ok=True)
        import soundfile as sf

        sf.write(str(wav_path), audio, 24000, subtype="PCM_16", format="WAV")
        duration = len(audio) / 24000
        logger.info("Saved %s reference audio: %.1fs -> %s", lang, duration, wav_path)
        return wav_path

    def generate(self, text: str, language: str) -> tuple[Any, int]:
        """Generate speech. Returns (numpy_float32_samples, sample_rate)."""
        from omnivoice import OmniVoiceGenerationConfig

        self._ensure_model()
        cleaned = " ".join(text.strip().split())

        voice_lang = language if language in self._voice_prompts else _DEFAULT_VOICE
        voice_prompt = self._voice_prompts.get(voice_lang)
        profile = _VOICE_PROFILES.get(voice_lang, _VOICE_PROFILES[_DEFAULT_VOICE])

        audio_arrays = self._model.generate(
            text=cleaned,
            language=language,
            voice_clone_prompt=voice_prompt,
            speed=profile["speed"],
            generation_config=OmniVoiceGenerationConfig(num_step=self._num_steps),
        )
        audio = audio_arrays[0]

        if audio.size == 0:
            logger.warning("OmniVoice returned empty audio for %r (lang=%s)", text[:40], language)
            import numpy as np

            audio = np.zeros(int(24000 * 0.3), dtype=np.float32)

        return audio, 24000


_FILLER_PHRASES = ["Ok.", "Hurmm.", "I'm thinking.", "Okaay."]


class VoiceEngineManager:
    """Owns the STT and TTS engine singletons. Loaded once at startup."""

    def __init__(self, settings: VoiceSettings) -> None:
        self.settings = settings
        self.stt = STTEngine(
            model_size=settings.stt_model,
            device=settings.stt_device,
            compute_type=settings.stt_compute_type,
        )
        self.tts = TTSEngine(num_steps=settings.tts_num_steps)
        self.filler_sounds: list[bytes] = []

    async def preload(self) -> None:
        """Load models and generate filler sounds. Call during lifespan startup."""
        logger.info("Preloading voice engines...")
        await asyncio.to_thread(self.stt.preload)
        await asyncio.to_thread(self.tts.preload, self.settings.voices_dir)
        self.filler_sounds = await asyncio.to_thread(self._generate_filler_sounds)
        logger.info("Voice engines ready (%d filler sounds)", len(self.filler_sounds))

    def _generate_filler_sounds(self) -> list[bytes]:
        fillers: list[bytes] = []
        for phrase in _FILLER_PHRASES:
            audio, sr = self.tts.generate(phrase, "en")
            if audio.size > 0:
                wav = samples_to_wav(audio, sr)
                fillers.append(wav)
        if not fillers:
            freq = 520.0
            for _ in _FILLER_PHRASES:
                fillers.append(generate_tone_wav(frequency=freq, duration=0.15, sample_rate=24000, amplitude=0.15))
                freq += 40
            logger.warning("All filler TTS failed, using ping tones as fallback")
        return fillers
