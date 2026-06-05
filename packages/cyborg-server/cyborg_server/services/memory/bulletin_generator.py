"""Bulletin generator — extracts memory-worthy bulletins from session transcripts.

Takes a compact input (session key, messages, participants) and uses an LLM
to identify distinct pieces of information worth remembering. Returns a list
of plain-text bulletin strings with contacts referenced as {{contact:ID|Name}} tags.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from cyborg_server.services.memory.models import BulletinGeneratorInput
from cyborg_server.services.memory.prompts import BULLETIN_GENERATION_PROMPT

logger = logging.getLogger(__name__)


def build_generator_input(
    *,
    session_key: str,
    messages: list[dict[str, str]],
    participants: list[dict[str, str]],
) -> BulletinGeneratorInput:
    """Construct a BulletinGeneratorInput from session metadata.

    Args:
        session_key: The session key.
        messages: List of dicts with keys sender_contact_id, timestamp, content.
        participants: List of dicts with keys id, name.
    """
    from cyborg_server.services.memory.models import BulletinMessage

    return BulletinGeneratorInput(
        session_key=session_key,
        messages=[
            BulletinMessage(
                sender_contact_id=m.get("sender_contact_id", "assistant"),
                timestamp=m.get("timestamp", ""),
                content=m.get("content", ""),
            )
            for m in messages
        ],
        participants=participants,
    )


def _format_user_prompt(input: BulletinGeneratorInput) -> str:
    """Build the user prompt from the input."""
    parts = [f"session_key: {input.session_key}"]

    if input.participants:
        parts.append("\nparticipants:")
        for p in input.participants:
            parts.append(f"  - id: {p['id']}, name: {p.get('name', p['id'])}")

    parts.append("\nmessages:")
    for msg in input.messages:
        sender = msg.sender_contact_id or "assistant"
        ts = msg.timestamp or ""
        content = msg.content[:500]
        if ts:
            parts.append(f"[{ts}] [{sender}]: {content}")
        else:
            parts.append(f"[{sender}]: {content}")

    return "\n".join(parts)


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    return text.strip()


async def generate_bulletins(
    llm: Any,
    input: BulletinGeneratorInput,
) -> list[str]:
    """Generate N plain-text bulletins from a session transcript.

    Returns a list of plain-text strings. Empty list if nothing is memory-worthy.
    """
    user_prompt = _format_user_prompt(input)

    response = await llm.chat(
        [
            {"role": "system", "content": BULLETIN_GENERATION_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        model=llm.memory_model,
        call_category="memory_bulletin_gen",
        temperature=0.2,
        max_tokens=2000,
    )

    try:
        parsed = json.loads(_strip_code_fences(response))
    except (json.JSONDecodeError, ValueError):
        logger.warning("Bulletin generator: failed to parse LLM response as JSON")
        return []

    if not isinstance(parsed, list):
        logger.warning("Bulletin generator: expected JSON array, got %s", type(parsed).__name__)
        return []

    return [s for s in parsed if isinstance(s, str) and s.strip()]
