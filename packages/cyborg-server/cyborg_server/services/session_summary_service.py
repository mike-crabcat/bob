"""Session idle summarization — generates summaries when sessions go idle."""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import uuid4

from cyborg_server.services.base import BaseService

logger = logging.getLogger(__name__)

_SUMMARY_SYSTEM_PROMPT = (
    "You are a conversation summarizer. Given a conversation transcript, produce a JSON summary.\n"
    'Return ONLY a JSON object with these keys:\n'
    '- "summary": exactly 2 sentences giving a high-level summary of the conversation.\n'
    '- "topics": array of short topic strings discussed.\n'
    '- "memory_prompts": array of specific facts or action items worth remembering.\n'
)


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    return text.strip()


class SessionSummaryService(BaseService):
    """Generates and stores session summaries for idle-active periods."""

    async def find_idle_sessions(
        self, idle_threshold_minutes: float = 5.0
    ) -> list[dict[str, Any]]:
        """Find sessions with unsaved messages that are now idle.

        Returns rows with: session_key, last_message_at, active_from, message_count.
        """
        rows = await self.db.fetch_all(
            """
            SELECT
                sm.session_key,
                MAX(sm.created_at) AS last_message_at,
                COALESCE(
                    ss.last_boundary,
                    (SELECT MIN(sm2.created_at) FROM session_messages sm2 WHERE sm2.session_key = sm.session_key)
                ) AS active_from,
                COUNT(*) AS message_count
            FROM session_messages sm
            LEFT JOIN (
                SELECT session_key, MAX(active_to) AS last_boundary
                FROM session_summaries
                GROUP BY session_key
            ) ss ON ss.session_key = sm.session_key
            WHERE sm.created_at > COALESCE(ss.last_boundary, '1970-01-01')
            GROUP BY sm.session_key
            HAVING MAX(sm.created_at) < datetime('now', '-' || ? || ' minutes')
            """,
            (idle_threshold_minutes,),
        )
        return [dict(r) for r in rows] if rows else []

    async def get_participant_name_map(
        self, session_key: str
    ) -> dict[str, str]:
        """Map sender_id (contact_id) to display_name for a session."""
        rows = await self.db.fetch_all(
            """SELECT contact_id, identifier, display_name FROM session_participants
               WHERE session_key = ?""",
            (session_key,),
        )
        if not rows:
            return {}
        name_map: dict[str, str] = {}
        for r in rows:
            name = r["display_name"]
            if not name:
                continue
            if r["contact_id"]:
                name_map[r["contact_id"]] = name
            name_map[r["identifier"]] = name
        return name_map

    async def get_messages_for_period(
        self, session_key: str, active_from: str, active_to: str
    ) -> list[dict[str, Any]]:
        """Fetch user/assistant messages from session_messages in the time window."""
        rows = await self.db.fetch_all(
            """SELECT role, content, sender_id FROM session_messages
               WHERE session_key = ? AND created_at > ? AND created_at <= ?
                 AND role IN ('user', 'assistant')
               ORDER BY created_at ASC""",
            (session_key, active_from, active_to),
        )
        return [dict(r) for r in rows] if rows else []

    async def get_participants_for_period(
        self, session_key: str, active_from: str, active_to: str
    ) -> list[str]:
        """Get participant display names active in the time window."""
        rows = await self.db.fetch_all(
            """SELECT DISTINCT display_name FROM session_participants
               WHERE session_key = ? AND last_active_at > ?""",
            (session_key, active_from),
        )
        if not rows:
            return []
        return [r["display_name"] for r in rows if r["display_name"]]

    async def generate_summary(
        self,
        messages: list[dict[str, Any]],
        participants: list[str],
        active_from: str,
        active_to: str,
        name_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Call the LLM to generate a summary. Returns {summary_text, topics, memory_prompts}."""
        from cyborg_server.services.llm_dispatch import LLMDispatchService

        resolved = name_map or {}

        def _speaker(m: dict[str, Any]) -> str:
            if m["role"] == "assistant":
                return "assistant"
            sender = m.get("sender_id")
            if sender and sender in resolved:
                return resolved[sender]
            return "user"

        conversation_text = "\n".join(
            f"[{_speaker(m)}] {m['content'][:500]}" for m in messages[-50:]
        )

        system = _SUMMARY_SYSTEM_PROMPT + (
            f"\nActive period: {active_from} to {active_to}\n"
            f"Participants: {', '.join(participants) if participants else 'unknown'}"
        )

        llm = LLMDispatchService(self.ctx)
        response = await llm.chat(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": f"Conversation transcript:\n\n{conversation_text}"},
            ],
            call_category="session_summary",
            temperature=0.3,
            max_tokens=500,
        )

        try:
            parsed = json.loads(_strip_code_fences(response))
            return {
                "summary_text": parsed.get("summary", ""),
                "topics": parsed.get("topics", []),
                "memory_prompts": parsed.get("memory_prompts", []),
            }
        except (json.JSONDecodeError, ValueError):
            logger.warning("Failed to parse LLM summary as JSON")
            return {
                "summary_text": (response or "")[:300],
                "topics": [],
                "memory_prompts": [],
            }

    async def store_summary(
        self,
        session_key: str,
        active_from: str,
        active_to: str,
        summary_text: str,
        topics: list[str],
        participants: list[str],
        memory_prompts: list[str],
        message_count: int,
        model_used: str,
    ) -> str:
        """Insert a summary row. Returns the summary ID."""
        summary_id = str(uuid4())
        await self.db.execute(
            """INSERT INTO session_summaries
               (id, session_key, active_from, active_to, summary_text, topics,
                participants, memory_prompts, message_count, model_used)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                summary_id,
                session_key,
                active_from,
                active_to,
                summary_text,
                json.dumps(topics),
                json.dumps(participants),
                json.dumps(memory_prompts),
                message_count,
                model_used,
            ),
        )
        return summary_id

    async def get_summaries(self, session_key: str) -> list[dict[str, Any]]:
        """Retrieve all summaries for a session, newest first."""
        rows = await self.db.fetch_all(
            """SELECT id, active_from, active_to, summary_text, topics,
                      participants, memory_prompts, message_count, model_used, created_at
               FROM session_summaries
               WHERE session_key = ?
               ORDER BY active_to DESC""",
            (session_key,),
        )
        if not rows:
            return []
        results = []
        for row in rows:
            r = dict(row)
            for field in ("topics", "participants", "memory_prompts"):
                raw = r.get(field, "[]")
                r[field] = json.loads(raw) if raw else []
            results.append(r)
        return results
