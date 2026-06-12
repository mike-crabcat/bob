"""WebSocket protocol message models for the voice chat subsystem."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel

# ---- Client → Server messages ----


class StartRecordingMessage(BaseModel):
    type: Literal["start_recording"]
    userId: str
    sessionKey: str
    language: str | None = None
    sessionMode: Literal["chat", "portuguese_teacher", "french_teacher", "beginner_french"] = "chat"


class StopRecordingMessage(BaseModel):
    type: Literal["stop_recording"]


class CancelMessage(BaseModel):
    type: Literal["cancel"]


class SetLanguageMessage(BaseModel):
    type: Literal["set_language"]
    language: str


class SessionHistoryMessage(BaseModel):
    type: Literal["session_history"]
    userId: str
    sessionMode: Literal["chat", "portuguese_teacher", "french_teacher", "beginner_french"] = "chat"


class ClearHistoryMessage(BaseModel):
    type: Literal["clear_history"]
    userId: str
    sessionMode: Literal["chat", "portuguese_teacher", "french_teacher", "beginner_french"] = "chat"


class ReplayTtsMessage(BaseModel):
    type: Literal["replay_tts"]
    text: str
    sessionMode: Literal["chat", "portuguese_teacher", "french_teacher", "beginner_french"] = "chat"


ClientMessage = (
    StartRecordingMessage
    | StopRecordingMessage
    | CancelMessage
    | SetLanguageMessage
    | SessionHistoryMessage
    | ClearHistoryMessage
    | ReplayTtsMessage
)

_MESSAGE_MAP: dict[str, type[ClientMessage]] = {
    "start_recording": StartRecordingMessage,
    "stop_recording": StopRecordingMessage,
    "cancel": CancelMessage,
    "set_language": SetLanguageMessage,
    "session_history": SessionHistoryMessage,
    "clear_history": ClearHistoryMessage,
    "replay_tts": ReplayTtsMessage,
}


def parse_client_message(raw: str) -> ClientMessage | None:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    msg_type = data.get("type")
    if msg_type not in _MESSAGE_MAP:
        return None
    return _MESSAGE_MAP[msg_type].model_validate(data)


# ---- Server → Client messages ----


class StatusMessage(BaseModel):
    type: Literal["status"] = "status"
    state: Literal["recording", "transcribing", "thinking", "speaking", "idle"]


class TranscriptMessage(BaseModel):
    type: Literal["transcript"] = "transcript"
    text: str
    language: str
    latency_ms: int


class PartialResponseMessage(BaseModel):
    type: Literal["partial_response"] = "partial_response"
    text: str


class ResponseTextMessage(BaseModel):
    type: Literal["response_text"] = "response_text"
    text: str


class LatencyMessage(BaseModel):
    type: Literal["latency"] = "latency"
    stt_ms: int | None = None
    llm_total_ms: int | None = None
    tts_first_chunk_ms: int | None = None
    e2e_ms: int | None = None


class AudioDoneMessage(BaseModel):
    type: Literal["audio_done"] = "audio_done"


class ErrorMessage(BaseModel):
    type: Literal["error"] = "error"
    message: str


class HistoryEntry(BaseModel):
    role: str
    text: str
    language: str | None = None


class HistoryMessage(BaseModel):
    type: Literal["history"] = "history"
    messages: list[HistoryEntry]


class LessonProgressMessage(BaseModel):
    type: Literal["lesson_progress"] = "lesson_progress"
    lessonNumber: int
    totalLessons: int
    completedSteps: list[int]
    totalSteps: int
