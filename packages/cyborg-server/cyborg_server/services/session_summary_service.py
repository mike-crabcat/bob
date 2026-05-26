"""Session idle summarization — generates summaries when sessions go idle."""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import uuid4

import re

from cyborg_server.services.base import BaseService

logger = logging.getLogger(__name__)

_SUMMARY_SYSTEM_PROMPT = (
    "You are a conversation summarizer. Given a conversation transcript, produce a JSON summary.\n"
    'Return ONLY a JSON object with these keys:\n'
    '- "summary": exactly 2 sentences giving a high-level summary of the conversation.\n'
    '- "topics": array of short topic strings discussed.\n'
    '- "memory_prompts": array of specific facts or action items worth remembering.\n'
    "\n"
    "IMPORTANT: Always use actual participant names — NEVER write \"the user\" or \"a participant\".\n"
    "The transcript labels each message with the speaker's name. Use those names in your output.\n"
    "For example, write \"Mike's cat is Aspen\" not \"The user's cat is Aspen.\"\n"
    "\n"
    "When referring to a person by name, use the contact reference format: {{contact:ID|Name}}\n"
    "For example, if contact ID 'abc123' has display name 'Mike', write: {{contact:abc123|Mike}}\n"
    "Use this format in summary, topics, and memory_prompts wherever a person's name appears.\n"
)

_CONTACT_REF_RE = re.compile(r"\{\{contact:([^|}]+)\|([^}]+)\}\}")


def _validate_refs(text: str, valid_ids: set[str]) -> str:
    """Remove contact refs with unknown IDs, keeping just the display name."""
    def _replace(m: re.Match[str]) -> str:
        if m.group(1) in valid_ids:
            return m.group(0)
        return m.group(2)
    return _CONTACT_REF_RE.sub(_replace, text)


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
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Return (contact_to_name, identifier_to_name) mappings."""
        rows = await self.db.fetch_all(
            """SELECT contact_id, identifier, display_name FROM session_participants
               WHERE session_key = ?""",
            (session_key,),
        )
        if not rows:
            return {}, {}
        contact_to_name: dict[str, str] = {}
        identifier_to_name: dict[str, str] = {}
        for r in rows:
            name = r["display_name"]
            if not name:
                continue
            if r["contact_id"]:
                contact_to_name[r["contact_id"]] = name
            identifier_to_name[r["identifier"]] = name
        return contact_to_name, identifier_to_name

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
        contact_to_name: dict[str, str] | None = None,
        identifier_to_name: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Call the LLM to generate a summary. Returns {summary_text, topics, memory_prompts}."""
        from cyborg_server.services.llm_dispatch import LLMDispatchService

        resolved = identifier_to_name or {}
        contact_map = contact_to_name or {}

        def _speaker(m: dict[str, Any]) -> str:
            if m["role"] == "assistant":
                return "assistant"
            sender = m.get("sender_id")
            if sender:
                if sender in resolved:
                    return resolved[sender]
                if sender in contact_map:
                    return contact_map[sender]
            return "user"

        conversation_text = "\n".join(
            f"[{_speaker(m)}] {m['content'][:500]}" for m in messages[-50:]
        )

        contact_lines = [
            f"  {cid} -> {name}" for cid, name in contact_map.items()
        ]
        contact_mapping = "\n".join(contact_lines) if contact_lines else "  (no contacts)"

        system = _SUMMARY_SYSTEM_PROMPT + (
            f"\nActive period: {active_from} to {active_to}\n"
            f"Participants: {', '.join(participants) if participants else 'unknown'}\n"
            f"Contact ID mappings:\n{contact_mapping}"
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
            valid_ids = set(contact_map)
            summary_text = _validate_refs(parsed.get("summary", ""), valid_ids)
            topics = [_validate_refs(t, valid_ids) for t in parsed.get("topics", [])]
            memory_prompts = [_validate_refs(p, valid_ids) for p in parsed.get("memory_prompts", [])]
            return {
                "summary_text": summary_text,
                "topics": topics,
                "memory_prompts": memory_prompts,
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
        if self.ctx.event_bus:
            await self.ctx.event_bus.publish("session.summary.created", {
                "id": summary_id,
                "session_key": session_key,
                "summary_text": summary_text,
                "topics": topics,
                "participants": participants,
            })
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
