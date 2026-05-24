"""Assembles prompt messages from workspace files and session history."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_WORKSPACE_FILES = ("SOUL.md", "IDENTITY.md", "AGENTS.md", "USER.md")

# Module-level cache for workspace file content.
_cached_prompt: tuple[Any, str] | None = None  # (mtime_hash, content)
_cached_mtime: dict[str, float] = {}


def load_workspace_prompt(workspace_dir: Path) -> str:
    """Load and concatenate workspace files. Cached until any file changes."""
    global _cached_prompt, _cached_mtime

    workspace_dir = workspace_dir.expanduser()
    mtimes: dict[str, float] = {}
    for name in _WORKSPACE_FILES:
        path = workspace_dir / name
        mtimes[name] = path.stat().st_mtime if path.is_file() else 0.0

    mtime_hash = tuple(mtimes.items())
    if _cached_prompt is not None and _cached_prompt[0] == mtime_hash:
        return _cached_prompt[1]

    parts: list[str] = []
    for name in _WORKSPACE_FILES:
        path = workspace_dir / name
        if path.is_file():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                parts.append(content)

    # Load skills index (lightweight — full skill loaded on-demand via use_skill tool)
    from cyborg_server.services.skill_loader import load_skills_index
    skills_index = load_skills_index(workspace_dir)
    if skills_index:
        parts.append("## Available Skills\n\n" + skills_index)

    # Load memory index for always-accessible wikis
    from cyborg_server.services.memory_service import MemoryService
    MemoryService.ensure_memory_structure(workspace_dir)
    memory_dir = workspace_dir / "memory"
    if memory_dir.is_dir():
        config = MemoryService.load_access_config(workspace_dir)
        always_wikis = [
            name for name, conf in config.get("wikis", {}).items()
            if conf.get("access") == "always"
        ]
        if always_wikis:
            mem_index = MemoryService._build_memory_index_static(workspace_dir, always_wikis)
            if mem_index:
                parts.append("## Memory\n\n" + mem_index)

    # Append grounding rules to reduce hallucinated tool claims
    parts.append(
        "## Grounding Rules\n"
        "- Only state that you have done something if you used a tool that confirmed success.\n"
        "- If you did not call a tool, the action did not happen — do not claim it did.\n"
        "- If a tool returns an error, report the error honestly — do not pretend it succeeded.\n"
        "- If you are unsure whether you can do something, say so. Do not claim capabilities you have not verified.\n"
        "- Your text output is NOT delivered to the user. Only tool calls have effect. "
        "Use as many tools as you need before replying — memory, files, docs, contacts, scripts. "
        "When ready to respond, you MUST call the channel send tool (send_whatsapp_message or email_reply) "
        "with your response. Without that call, nothing is sent."
    )

    combined = "\n\n".join(parts)
    _cached_prompt = (mtime_hash, combined)
    if _cached_mtime != mtimes:
        logger.info(
            "Workspace loaded: dir=%s chars=%d files=%s",
            workspace_dir, len(combined),
            [n for n in _WORKSPACE_FILES if mtimes.get(n)],
        )
        _cached_mtime.update(mtimes)
    return combined


async def build_chat_messages(
    user_message: str | None = None,
    session_key: str = "",
    *,
    db: Any = None,
    system_content: str = "",
    voice_instructions: str = "",
    max_history: int = 20,
) -> list[dict[str, str]]:
    """Build a messages array: system prompt + session history + optional user message."""
    system_parts: list[str] = []
    if system_content:
        system_parts.append(system_content)
    if voice_instructions:
        system_parts.append(voice_instructions)

    messages: list[dict[str, str]] = []
    if system_parts:
        messages.append({"role": "system", "content": "\n\n".join(system_parts)})

    if session_key and db is not None:
        # Use a lightweight approach — just query directly
        rows = await db.fetch_all(
            "SELECT role, content FROM session_messages "
            "WHERE session_key = ? AND role IN ('user', 'assistant') "
            "AND rowid IN (SELECT rowid FROM session_messages "
            "WHERE session_key = ? AND role IN ('user', 'assistant') "
            "ORDER BY created_at DESC LIMIT ?) "
            "ORDER BY created_at ASC",
            (session_key, session_key, max_history),
        )
        for row in rows:
            if row["content"]:
                messages.append({"role": row["role"], "content": row["content"]})

    if user_message is not None:
        messages.append({"role": "user", "content": user_message})
    return messages
