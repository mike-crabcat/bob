"""Session reflection — dispatches an LLM call to analyze session history."""

from __future__ import annotations

import json
import logging
from typing import Any

from cyborg_server.context import AppContext
from cyborg_server.services.base import BaseService
from cyborg_server.services.tools import Tool, tool

logger = logging.getLogger(__name__)

_REFLECTION_SYSTEM_PROMPT = (
    "You are an expert agent engineer analyzing a cyborg agent session. "
    "You have access to the full session history below.\n\n"
    "Your job is to answer the user's question by carefully examining the conversation transcript, "
    "LLM calls, tool invocations, and agent behavior. Be factual and technical. "
    "Cite specific calls or messages when relevant. If the agent made an error or missed something, "
    "explain what happened and why. If the user asks why something wasn't done, "
    "trace through the calls to determine the reasoning.\n\n"
    "Be concise but thorough. Use bullet points for multi-part answers."
)

_TRUNCATE_MSG = 500
_TRUNCATE_SESSION_MSG = 300
_MAX_CALLS = 30
_MAX_MESSAGES = 50


class ReflectionService(BaseService):
    """Dispatches reflection LLM calls that analyze session history."""

    async def reflect(self, session_key: str, query: str) -> dict[str, Any]:
        from cyborg_server.services.llm_dispatch import LLMDispatchService

        transcript = await self._build_transcript(session_key)
        system = _REFLECTION_SYSTEM_PROMPT + f"\n\n---\nSession transcript:\n\n{transcript}"

        llm = LLMDispatchService(self.ctx)
        response = await llm.chat(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": query},
            ],
            call_category="reflection",
            session_key=session_key,
            temperature=0.3,
        )

        return {
            "response_text": response or "",
        }

    async def _build_transcript(self, session_key: str) -> str:
        parts: list[str] = []

        # LLM call history — get the latest N, then display chronologically
        call_rows = await self.db.fetch_all(
            """SELECT * FROM (
                   SELECT id, call_category, status, model, user_message, response_text,
                          error_message, messages_json, created_at
                   FROM llm_call_log
                   WHERE session_key = ?
                   ORDER BY created_at DESC
                   LIMIT ?
               ) ORDER BY created_at ASC""",
            (session_key, _MAX_CALLS),
        )
        if call_rows:
            parts.append(f"## LLM Calls ({len(call_rows)} most recent)")
            for i, row in enumerate(call_rows, 1):
                um = (row["user_message"] or "")[:_TRUNCATE_MSG]
                rt = (row["response_text"] or "")[:_TRUNCATE_MSG]
                err = row["error_message"]
                parts.append(
                    f"[{i}] {row['call_category']} | {row['status']} | {row['model']} | {row['created_at']}\n"
                    f"    User: {um}\n"
                    f"    Response: {rt}"
                    + (f"\n    Error: {err}" if err else "")
                )
                # Extract tool calls from messages_json
                tool_log = self._extract_tool_log(row.get("messages_json"))
                if tool_log:
                    parts.append(f"    Tool calls:\n{tool_log}")

        # Session messages
        msg_rows = await self.db.fetch_all(
            """SELECT role, content FROM session_messages
               WHERE session_key = ?
               ORDER BY created_at ASC
               LIMIT ?""",
            (session_key, _MAX_MESSAGES),
        )
        if msg_rows:
            parts.append(f"\n## Session Messages ({len(msg_rows)} total)")
            for row in msg_rows:
                content = (row["content"] or "")[:_TRUNCATE_SESSION_MSG]
                parts.append(f"[{row['role']}] {content}")

        return "\n".join(parts) if parts else "(no session history found)"

    @staticmethod
    def _extract_tool_log(messages_json: str | None) -> str:
        """Parse messages_json and extract tool call arguments + outputs."""
        if not messages_json:
            return ""
        try:
            messages = json.loads(messages_json)
        except (json.JSONDecodeError, TypeError):
            return ""
        entries: list[str] = []
        for msg in messages:
            if isinstance(msg, dict):
                if msg.get("type") == "function_call":
                    name = msg.get("name", "?")
                    args = msg.get("arguments", "")
                    if len(args) > 200:
                        args = args[:200] + "..."
                    entries.append(f"      -> {name}({args})")
                elif msg.get("type") == "function_call_output":
                    output = msg.get("output", "")
                    if len(output) > 200:
                        output = output[:200] + "..."
                    entries.append(f"      <- {output}")
        return "\n".join(entries)


def make_reflection_tools(ctx: AppContext, session_key: str) -> list[Tool]:
    """Create the reflect_on_session tool for trusted contacts."""
    svc = ReflectionService(ctx)

    @tool
    async def reflect_on_session(question: str) -> str:
        """Analyze this session's history to answer a meta-question about agent behavior.
        Use this when the user asks WHY something was done (or not done), why a response was given,
        why a tool was or wasn't used, or wants to understand agent reasoning.
        The question should be specific, e.g. 'why did you not post the image?' or
        'why did you send that reply to John?'."""
        result = await svc.reflect(session_key, question)
        return result.get("response_text", "No analysis available.")

    return [reflect_on_session]
