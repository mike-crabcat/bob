"""Async voice session persistence backed by the bob database."""

from __future__ import annotations

import logging
from typing import Any

from bob_server.services.base import BaseService, utcnow

logger = logging.getLogger(__name__)


class VoiceSessionStore(BaseService):
    """Manages voice chat session messages and lesson progress."""

    async def add_message(
        self, session_key: str, role: str, text: str, language: str | None = None,
    ) -> None:
        from bob_server.services.session_service import SessionService
        svc = SessionService(self.ctx)
        metadata = {}
        if language:
            metadata["language"] = language
        await svc.add_message(
            session_key, role, text,
            channel="voice",
            metadata=metadata or None,
        )

    async def get_messages(
        self, session_key: str, limit: int = 200,
    ) -> list[dict[str, str | None]]:
        from bob_server.services.session_service import SessionService
        svc = SessionService(self.ctx)
        msgs = await svc.get_messages(session_key, limit=limit)
        return [{"role": m.role, "text": m.content, "language": m.metadata.get("language")} for m in msgs]

    async def delete_session(self, session_key: str) -> None:
        from bob_server.services.session_service import SessionService
        svc = SessionService(self.ctx)
        await svc.delete_session(session_key)

    async def delete_old_sessions(self, max_age_days: int = 30) -> int:
        from datetime import timedelta

        cutoff = (utcnow() - timedelta(days=max_age_days)).isoformat()
        count = await self.db.execute(
            "DELETE FROM session_messages WHERE created_at < ? AND channel = 'voice'",
            (cutoff,),
        )
        if count:
            logger.info("Purged %d old session messages (older than %d days)", count, max_age_days)
        return count

    # ---- Lesson progress ----

    async def get_current_lesson(self, user_id: str, mode: str, total_lessons: int) -> int:
        row = await self.db.fetch_one(
            "SELECT lesson_number FROM voice_current_lesson WHERE user_id = ? AND mode = ?",
            (user_id, mode),
        )
        if row:
            return min(row["lesson_number"], total_lessons)
        return 1

    async def set_current_lesson(self, user_id: str, mode: str, lesson_number: int) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO voice_current_lesson (user_id, mode, lesson_number) VALUES (?, ?, ?)",
            (user_id, mode, lesson_number),
        )

    async def mark_step_complete(self, user_id: str, mode: str, lesson: int, step: int) -> None:
        now = utcnow().isoformat()
        await self.db.execute(
            "INSERT OR REPLACE INTO voice_lesson_progress (user_id, mode, lesson_number, step_index, completed, completed_at) VALUES (?, ?, ?, ?, 1, ?)",
            (user_id, mode, lesson, step, now),
        )

    async def get_completed_steps(self, user_id: str, mode: str, lesson: int) -> list[int]:
        rows = await self.db.fetch_all(
            "SELECT step_index FROM voice_lesson_progress WHERE user_id = ? AND mode = ? AND lesson_number = ? AND completed = 1 ORDER BY step_index",
            (user_id, mode, lesson),
        )
        return [row["step_index"] for row in rows]

    async def advance_lesson(self, user_id: str, mode: str, total_lessons: int) -> int:
        current = await self.get_current_lesson(user_id, mode, total_lessons)
        next_lesson = min(current + 1, total_lessons)
        await self.set_current_lesson(user_id, mode, next_lesson)
        return next_lesson

    async def reset_lesson(self, user_id: str, mode: str, lesson: int) -> None:
        await self.db.execute(
            "DELETE FROM voice_lesson_progress WHERE user_id = ? AND mode = ? AND lesson_number = ?",
            (user_id, mode, lesson),
        )

    async def reset_all_lessons(self, user_id: str, mode: str) -> None:
        await self.db.execute(
            "DELETE FROM voice_lesson_progress WHERE user_id = ? AND mode = ?",
            (user_id, mode),
        )
        await self.db.execute(
            "DELETE FROM voice_current_lesson WHERE user_id = ? AND mode = ?",
            (user_id, mode),
        )
