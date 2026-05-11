"""Voice chat audio processing pipeline with dispatch system integration.

Each voice interaction is tracked as a dispatch for lifecycle management
(concurrency limiting, stuck detection, auto-completion).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any

from cyborg_server.context import AppContext
from cyborg_server.services.base import BaseService, utcnow
from cyborg_server.services.voice_engines import VoiceEngineManager, samples_to_wav
from cyborg_server.services.voice_protocol import (
    AudioDoneMessage,
    LatencyMessage,
    PartialResponseMessage,
    ResponseTextMessage,
)
from cyborg_server.services.voice_session_store import VoiceSessionStore
from cyborg_server.services.voice_transport import VoiceTransport

logger = logging.getLogger(__name__)

_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")
_LANG_TAG_RE = re.compile(r"<lang\s+(\w+)>(.*?)</lang>", re.DOTALL)
_STEP_COMPLETE_RE = re.compile(r'<step_complete\s+lesson="(\d+)"\s+step="(\d+)"\s*/?>')
_LESSON_COMPLETE_RE = re.compile(r'<lesson_complete\s+lesson="(\d+)"\s*/?>')
_HANGUP_RE = re.compile(r"<hangup\s*/?>")

_LANGUAGE_NAMES: dict[str, str] = {
    "pt": "Portuguese", "es": "Spanish", "fr": "French", "de": "German",
    "it": "Italian", "nl": "Dutch", "ja": "Japanese", "ko": "Korean",
    "zh": "Chinese", "ru": "Russian", "ar": "Arabic", "hi": "Hindi",
}

_TOTAL_LESSONS = 10


def _session_key(user_id: str, session_mode: str) -> str:
    return f"bobvoice:{session_mode}:{user_id}"


def _process_language_tags(text: str, default_lang: str) -> list[tuple[str, str]]:
    """Split text at <lang> tag boundaries into (text, lang) TTS fragments."""
    fragments: list[tuple[str, str]] = []
    last_end = 0
    for m in _LANG_TAG_RE.finditer(text):
        before = text[last_end:m.start()]
        if before.strip():
            fragments.append((before.strip(), default_lang))
        tagged_text = m.group(2).strip()
        if tagged_text:
            fragments.append((tagged_text, m.group(1)))
        last_end = m.end()
    remaining = text[last_end:]
    if remaining.strip():
        fragments.append((remaining.strip(), default_lang))
    return fragments if fragments else [(text, default_lang)]


def _extract_instruction_tokens(text: str) -> tuple[str, list[tuple[int, int]], list[int]]:
    """Extract step_complete and lesson_complete tokens. Returns (cleaned_text, steps, lessons)."""
    steps = [(int(m.group(1)), int(m.group(2))) for m in _STEP_COMPLETE_RE.finditer(text)]
    lessons = [int(m.group(1)) for m in _LESSON_COMPLETE_RE.finditer(text)]
    cleaned = _STEP_COMPLETE_RE.sub("", text)
    cleaned = _LESSON_COMPLETE_RE.sub("", cleaned)
    cleaned = _HANGUP_RE.sub("", cleaned)
    return cleaned, steps, lessons


def _clean_display_text(text: str) -> str:
    """Strip all non-display markup: <lang> tags, step_complete, lesson_complete, hangup."""
    text = _LANG_TAG_RE.sub(r"\2", text)
    text = _STEP_COMPLETE_RE.sub("", text)
    text = _LESSON_COMPLETE_RE.sub("", text)
    text = _HANGUP_RE.sub("", text)
    return text


def _common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n


class VoiceService(BaseService):
    """Orchestrates the voice chat pipeline using the dispatch system."""

    def __init__(self, ctx: AppContext, engines: VoiceEngineManager) -> None:
        super().__init__(ctx)
        self.engines = engines
        self._session_store = VoiceSessionStore(ctx)

    def _lessons_dir(self) -> Path | None:
        voice = self.ctx.settings.voice
        return voice.lessons_dir

    def _prompts_dir(self) -> Path | None:
        lessons = self._lessons_dir()
        return lessons.parent / "prompts" if lessons else None

    async def process_audio(
        self,
        transport: VoiceTransport,
        audio_chunks: list[bytes],
        language: str | None,
        user_id: str = "mike",
        session_mode: str = "chat",
        agenda: str = "",
        warmup_ok: bool = False,
    ) -> None:
        """Run the full voice pipeline: STT → LLM dispatch → TTS, tracked as a dispatch."""
        audio_data = b"".join(audio_chunks)
        if not audio_data:
            await transport.send_error("No audio data received")
            return

        await transport.send_status("transcribing")
        t0 = time.monotonic()

        # --- STT ---
        try:
            text, detected_lang = await asyncio.to_thread(
                self.engines.stt.transcribe, audio_data, language,
            )
        except Exception:
            logger.error("STT transcription failed", exc_info=True)
            await transport.send_error("Transcription failed")
            return
        stt_ms = int((time.monotonic() - t0) * 1000)

        if not text:
            await transport.send_error("Could not transcribe audio")
            return

        await transport.send_message("transcript", {
            "text": text, "language": detected_lang, "latency_ms": stt_ms,
        })

        # --- Dispatch through the dispatch system ---
        await transport.send_status("thinking")

        is_beginner = session_mode == "beginner_french"
        default_tts_lang = "en" if is_beginner else detected_lang

        lesson_context = await self._build_lesson_context(user_id, session_mode) if is_beginner else None

        session_key = _session_key(user_id, session_mode)

        from cyborg_server.services.dispatch_service import DispatchService

        dispatch_service = DispatchService(self.ctx)
        from cyborg_server.models import DispatchCategory

        dispatch_id = await dispatch_service.record_dispatch(
            notification_type=DispatchCategory.VOICE_CHAT.value,
            session_key=session_key,
        )

        coro = self._voice_dispatch_coro(
            transport=transport,
            text=text,
            detected_lang=detected_lang,
            session_key=session_key,
            session_mode=session_mode,
            user_id=user_id,
            is_beginner=is_beginner,
            default_tts_lang=default_tts_lang,
            lesson_context=lesson_context,
            t0=t0,
            stt_ms=stt_ms,
            agenda=agenda,
            warmup_ok=warmup_ok,
            dispatch_id=dispatch_id,
        )
        dispatch_service.track(dispatch_id, coro)

    async def _voice_dispatch_coro(
        self,
        *,
        transport: VoiceTransport,
        text: str,
        detected_lang: str,
        session_key: str,
        session_mode: str,
        user_id: str,
        is_beginner: bool,
        default_tts_lang: str,
        lesson_context: str | None,
        t0: float,
        stt_ms: int,
        agenda: str = "",
        warmup_ok: bool = False,
        dispatch_id: str = "",
    ) -> None:
        """Coroutine that runs the streaming gateway call + TTS pipeline.

        Wrapped by DispatchService.track() for lifecycle management.
        """
        t1 = time.monotonic()

        # Timing breakdown
        latency: dict[str, int] = {"stt_ms": stt_ms}
        t_dispatch_start = t1

        sentence_queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()
        speech_buffer = ""
        tts_pos = 0
        tts_first_chunk_ms: int | None = None
        last_accumulated = ""
        filler_index = 0
        cancel_requested = asyncio.Event()

        async def on_tool_start() -> None:
            nonlocal filler_index
            wav = self.engines.filler_sounds[filler_index % len(self.engines.filler_sounds)]
            filler_index += 1
            try:
                await transport.send_audio(wav)
            except Exception:
                pass

        async def on_delta(accumulated: str) -> None:
            nonlocal speech_buffer, tts_pos, last_accumulated
            if accumulated == last_accumulated:
                return
            if not last_accumulated:
                speech_buffer += accumulated
            elif accumulated.startswith(last_accumulated):
                speech_buffer += accumulated[len(last_accumulated):]
            else:
                common = _common_prefix_len(last_accumulated, accumulated)
                revised = accumulated[common:]
                if revised.strip():
                    speech_buffer += " I mean, " + revised

            last_accumulated = accumulated

            while tts_pos < len(speech_buffer):
                unspoken = speech_buffer[tts_pos:]
                match = None
                search_start = 0
                while True:
                    candidate = _SENTENCE_END.search(unspoken, search_start)
                    if candidate is None:
                        break
                    prefix = unspoken[:candidate.end()]
                    if prefix.count("<lang ") == prefix.count("</lang>"):
                        match = candidate
                        break
                    search_start = candidate.end()
                if match is None:
                    break
                sentence = unspoken[:match.end()]
                tts_pos += match.end()
                clean_sentence = _STEP_COMPLETE_RE.sub("", sentence)
                clean_sentence = _LESSON_COMPLETE_RE.sub("", clean_sentence)
                clean_sentence = _HANGUP_RE.sub("", clean_sentence)
                fragments = _process_language_tags(clean_sentence, default_tts_lang)
                for frag_text, frag_lang in fragments:
                    await sentence_queue.put((frag_text, frag_lang))
                try:
                    await transport.send_message("partial_response", {
                        "text": _clean_display_text(accumulated),
                    })
                except Exception:
                    pass

        async def tts_consumer() -> None:
            nonlocal tts_first_chunk_ms
            prev_send: asyncio.Task | None = None
            while not cancel_requested.is_set():
                try:
                    item = await asyncio.wait_for(sentence_queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                if item is None:
                    break
                sentence_text, sentence_lang = item
                try:
                    t_before_lock = time.monotonic()
                    async with self.engines.tts.lock:
                        t_lock_acquired = time.monotonic()
                        audio, sr = await asyncio.to_thread(
                            self.engines.tts.generate, sentence_text, sentence_lang,
                        )
                        t_tts_done = time.monotonic()
                        wav_bytes = samples_to_wav(audio, sr)
                    logger.info(
                        "TTS: %.0fms (+encode %.0fms) for %r",
                        (t_tts_done - t_lock_acquired) * 1000,
                        (time.monotonic() - t_tts_done) * 1000,
                        sentence_text[:60],
                    )
                    if tts_first_chunk_ms is None:
                        tts_first_chunk_ms = int((time.monotonic() - t1) * 1000)
                        latency["tts_first_chunk_ms"] = tts_first_chunk_ms
                        latency["tts_wait_lock_ms"] = int((t_lock_acquired - t_before_lock) * 1000)
                        latency["tts_generate_ms"] = int((t_tts_done - t_lock_acquired) * 1000)
                        try:
                            await transport.send_status("speaking")
                        except Exception:
                            pass
                    if prev_send is not None:
                        await prev_send
                    prev_send = asyncio.create_task(transport.send_audio(wav_bytes))
                except Exception:
                    logger.warning("TTS failed for %r (lang=%s)", sentence_text[:80], sentence_lang, exc_info=True)
            if prev_send is not None:
                await prev_send

        tts_task = asyncio.create_task(tts_consumer())

        # --- LLM streaming call ---
        from cyborg_server.services.prompt_assembler import load_workspace_prompt, build_chat_messages
        from cyborg_server.services.llm_dispatch import LLMDispatchService

        settings = self._get_settings()
        t_gateway_connect = time.monotonic()
        t_prepare = t_gateway_connect
        t_stream_start = t_gateway_connect
        try:
            voice_instructions = ""
            message = text

            if lesson_context:
                message = f"{lesson_context} {text}"
            elif session_mode.endswith("_teacher"):
                prompt = self._load_prompt_template(session_mode)
                message = f"{prompt} {text}" if prompt else text
            else:
                voice_instructions = (
                    "You are participating in a live voice conversation. "
                    "Respond in plain spoken language: no emojis, no markdown formatting, "
                    "no asterisks, no bullet points. Just natural speech."
                )
                if agenda:
                    voice_instructions += f"\n\nCALL AGENDA: {agenda}. Follow this agenda throughout the conversation. Stay on topic and work toward the agenda's goal."

            # Check for stored session agenda
            from cyborg_server.services.session_agenda_service import SessionAgendaService
            session_agenda = await SessionAgendaService(self.ctx).get_agenda(session_key)
            if session_agenda and not agenda:
                voice_instructions += f"\n\nAGENDA: {session_agenda}"

            if detected_lang and detected_lang != "en":
                    lang_name = _LANGUAGE_NAMES.get(detected_lang, detected_lang)
                    voice_instructions += f"\n\nRespond in {lang_name}. Act as a language coach: suggest corrections to the user's grammar and phrasing when they make mistakes."

            workspace_prompt = load_workspace_prompt(settings.harness.workspace_dir)
            messages = await build_chat_messages(
                message, session_key,
                db=self.db,
                system_content=workspace_prompt,
                voice_instructions=voice_instructions,
                max_history=settings.harness.max_history_messages,
            )

            dispatch = LLMDispatchService(self.ctx)
            t_prepare = time.monotonic()
            t_stream_start = t_prepare
            accumulated = ""

            # Build workspace tools for file access during conversation
            from cyborg_server.services.workspace_tools import make_workspace_tools
            tools = make_workspace_tools(self.ctx, session_key=session_key)

            async for chunk in dispatch.chat_stream_with_tools(
                messages,
                tools=tools,
                provider="openai",
                model=settings.harness.default_model,
                call_category="voice_chat",
                session_key=session_key,
                dispatch_id=dispatch_id,
            ):
                if chunk:
                    accumulated += chunk
                    await on_delta(accumulated)
                    if on_tool_start and len(accumulated) < 20:
                        await on_tool_start()
            response = accumulated
        except Exception:
            logger.exception("LLM dispatch error during voice dispatch")
            response = "Sorry, I couldn't reach the AI service."

        t_gateway_done = time.monotonic()
        llm_ms = int((t_gateway_done - t1) * 1000)
        latency["llm_total_ms"] = llm_ms
        latency["gateway_prepare_ms"] = int((t_prepare - t_gateway_connect) * 1000)
        latency["gateway_stream_ms"] = int((t_gateway_done - t_stream_start) * 1000)
        latency["first_audio_at_ms"] = tts_first_chunk_ms or 0
        logger.info(
            "Voice pipeline timing: STT=%dms prepare=%dms stream=%dms llm_total=%dms tts_first_chunk=%dms",
            stt_ms,
            latency["gateway_prepare_ms"],
            latency["gateway_stream_ms"],
            llm_ms,
            tts_first_chunk_ms or 0,
        )

        if last_accumulated:
            response = last_accumulated

        clean_response, completed_steps_raw, completed_lessons = _extract_instruction_tokens(response)

        # Flush remaining speech buffer
        remaining = speech_buffer[tts_pos:]
        if remaining.strip():
            tts_pos = len(speech_buffer)
            clean_remaining = _STEP_COMPLETE_RE.sub("", remaining)
            clean_remaining = _LESSON_COMPLETE_RE.sub("", clean_remaining)
            clean_remaining = _HANGUP_RE.sub("", clean_remaining)
            if clean_remaining.strip():
                fragments = _process_language_tags(clean_remaining, default_tts_lang)
                for frag_text, frag_lang in fragments:
                    await sentence_queue.put((frag_text, frag_lang))

        await sentence_queue.put(None)
        await tts_task

        # Signal hangup if agent requested it
        if _HANGUP_RE.search(response):
            try:
                await transport.send_message("hangup", {})
            except Exception:
                pass

        # --- Persist session ---
        if is_beginner:
            for lesson_num, step_idx in completed_steps_raw:
                await self._session_store.mark_step_complete(user_id, "beginner_french", lesson_num, step_idx)
            for lesson_num in completed_lessons:
                new_lesson = await self._session_store.advance_lesson(user_id, "beginner_french", _TOTAL_LESSONS)
                await self._session_store.reset_lesson(user_id, "beginner_french", new_lesson)

        await self._session_store.add_message(session_key, "user", text, language=detected_lang)
        await self._session_store.add_message(session_key, "assistant", clean_response)

        # --- Send final messages ---
        if tts_first_chunk_ms is None:
            tts_first_chunk_ms = int((time.monotonic() - t1) * 1000)

        await transport.send_message("response_text", {
            "text": _clean_display_text(clean_response),
        })

        e2e_ms = int((time.monotonic() - t0) * 1000)
        latency["e2e_ms"] = e2e_ms
        await transport.send_message("audio_done", {})
        await transport.send_message("latency", latency)
        await transport.send_status("idle")

    def _load_prompt_template(self, name: str) -> str | None:
        prompts_dir = self._prompts_dir()
        if not prompts_dir:
            return None
        path = prompts_dir / f"{name}.txt"
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
        return None

    async def _build_lesson_context(self, user_id: str, session_mode: str) -> str | None:
        lessons_dir = self._lessons_dir()
        if not lessons_dir:
            return None

        current_lesson = await self._session_store.get_current_lesson(user_id, session_mode, _TOTAL_LESSONS)
        lesson_path = lessons_dir / f"beginner_french_{current_lesson:02d}.md"
        if not lesson_path.is_file():
            current_lesson = 1
            lesson_path = lessons_dir / "beginner_french_01.md"

        lesson_content = lesson_path.read_text(encoding="utf-8")
        completed_steps = await self._session_store.get_completed_steps(user_id, session_mode, current_lesson)

        prompts_dir = self._prompts_dir()
        if not prompts_dir:
            return None
        template_path = prompts_dir / "beginner_french.txt"
        if not template_path.is_file():
            return None

        template = template_path.read_text(encoding="utf-8").strip()
        steps_str = ", ".join(str(s) for s in completed_steps) if completed_steps else "none"
        return template.format(
            LESSON_NUMBER=current_lesson,
            LESSON_CONTENT=lesson_content,
            COMPLETED_STEPS_LIST=steps_str,
            USER_ID=user_id,
        )

    async def replay_tts(self, transport: VoiceTransport, text: str, default_lang: str) -> None:
        """Replay TTS for a given text (e.g., replay button)."""
        fragments = _process_language_tags(text, default_lang)
        for frag_text, frag_lang in fragments:
            try:
                async with self.engines.tts.lock:
                    audio, sr = await asyncio.to_thread(
                        self.engines.tts.generate, frag_text, frag_lang
                    )
                    wav_bytes = samples_to_wav(audio, sr)
                await transport.send_audio(wav_bytes)
            except Exception:
                logger.warning("Replay TTS failed for %r (lang=%s)", frag_text[:80], frag_lang, exc_info=True)
        await transport.send_message("audio_done", {})
