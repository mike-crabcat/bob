"""Bulletin generator — converts a session transcript range into a draft bulletin.

The generator invokes the LLM with the bulletin generation system prompt and
structured input derived from a session transcript range.  It returns the raw
LLM response (either a no-bulletin markdown or a draft bulletin markdown) and
provides helpers for input construction and output validation.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import yaml

from cyborg_server.services.memory.channels import (
    derive_channel_type,
    derive_scope,
    derive_visibility,
    resolve_channel_id,
)
from cyborg_server.services.memory.models import BulletinGeneratorInput
from cyborg_server.services.memory.prompts import BULLETIN_GENERATION_PROMPT

# Required frontmatter fields for a draft bulletin that has create_bulletin: true
_REQUIRED_BULLETIN_FIELDS = (
    "create_bulletin",
    "session_id",
    "channel_id",
    "visibility",
    "scope",
    "entities",
    "confidence",
    "requires_review",
)

# Required fields for a no-bulletin response
_REQUIRED_NO_BULLETIN_FIELDS = (
    "create_bulletin",
    "reason",
    "session_id",
)

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from the LLM response text.

    Returns (frontmatter_dict, body).  If no frontmatter fence is found,
    returns ({}, text).
    """
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    fm = yaml.safe_load(m.group(1)) or {}
    body = text[m.end():].lstrip("\n")
    return fm, body


def _format_user_prompt(input: BulletinGeneratorInput) -> str:
    """Build the user-prompt string from a BulletinGeneratorInput.

    The user prompt presents all input fields in YAML-like format followed
    by the raw transcript text, matching the structure the system prompt
    expects.
    """
    import yaml as _yaml  # reuse the already-imported yaml

    known_entities_block = ""
    if input.known_entities:
        known_entities_block = _yaml.dump(
            {"known_entities": input.known_entities},
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )

    parts: list[str] = [
        f"session_id: {input.session_id}",
        f"transcript_range_id: {input.transcript_range_id}",
        f"transcript_start_time: {input.transcript_start_time.isoformat()}",
        f"transcript_end_time: {input.transcript_end_time.isoformat()}",
        f"channel_id: {input.channel_id}",
        f"channel_type: {input.channel_type}",
        f"source_type: {input.source_type}",
    ]

    if input.actor_contact_id:
        parts.append(f"actor_contact_id: {input.actor_contact_id}")

    parts.append(f"visibility: {input.visibility}")

    if input.scope:
        scope_yaml = _yaml.dump(
            {"scope": input.scope},
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )
        parts.append(scope_yaml.rstrip("\n"))

    if known_entities_block:
        parts.append(known_entities_block.rstrip("\n"))

    parts.append("")
    parts.append("transcript: |")
    for line in input.transcript.splitlines():
        parts.append(f"  {line}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def generate_bulletin(
    llm: Any,
    input: BulletinGeneratorInput,
) -> str:
    """Generate a draft bulletin from a session transcript range.

    Args:
        llm: An LLMDispatchService instance with a ``chat()`` method.
        input: Structured input for the bulletin generator.

    Returns:
        The raw LLM response text — either a no-bulletin response or a
        draft bulletin in markdown with YAML frontmatter.
    """
    user_prompt = _format_user_prompt(input)

    messages = [
        {"role": "system", "content": BULLETIN_GENERATION_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    response = await llm.chat(
        messages,
        model=llm.memory_model,
        call_category="memory_bulletin_gen",
        temperature=0.2,
        max_tokens=2000,
    )

    return response


def validate_draft_bulletin(text: str) -> tuple[bool, dict]:
    """Validate a draft bulletin response from the LLM.

    Parses the YAML frontmatter and checks required fields.

    Returns:
        A tuple of (is_valid, data):
        - If the response is a valid no-bulletin:
          (True, {"create_bulletin": False, "reason": ...})
        - If the response is a valid draft bulletin:
          (True, parsed_frontmatter_dict)
        - If validation fails:
          (False, {"error": "description of what went wrong"})
    """
    fm, _body = _parse_frontmatter(text)

    if not fm:
        return False, {"error": "No YAML frontmatter found in response."}

    create_bulletin = fm.get("create_bulletin")

    if create_bulletin is None:
        return False, {"error": "Missing required field: create_bulletin."}

    # --- No-bulletin path ---
    if create_bulletin is False:
        missing = [f for f in _REQUIRED_NO_BULLETIN_FIELDS if f not in fm]
        if missing:
            return False, {"error": f"No-bulletin response missing fields: {missing}."}
        return True, {
            "create_bulletin": False,
            "reason": fm.get("reason", "No reason provided."),
            "session_id": fm.get("session_id"),
        }

    # --- Draft bulletin path ---
    if create_bulletin is True:
        missing = [f for f in _REQUIRED_BULLETIN_FIELDS if f not in fm]
        if missing:
            return False, {"error": f"Draft bulletin missing required fields: {missing}."}
        return True, fm

    # create_bulletin has an unexpected value
    return False, {
        "error": f"Unexpected value for create_bulletin: {create_bulletin!r}."
    }


def build_generator_input(
    session_key: str,
    transcript_start: str,
    transcript_end: str,
    transcript_text: str,
    contact_ids: list[str] | None = None,
    known_entities: dict | None = None,
) -> BulletinGeneratorInput:
    """Construct a BulletinGeneratorInput from session metadata.

    Derives channel_id, visibility, scope, and channel_type from the
    session_key using the channel adapters.

    Args:
        session_key: The session key (e.g. ``agent:main:whatsapp:group:120363...``).
        transcript_start: ISO-8601 timestamp string for the start of the range.
        transcript_end: ISO-8601 timestamp string for the end of the range.
        transcript_text: The raw transcript text for the range.
        contact_ids: Optional list of contact IDs for scope derivation.
        known_entities: Optional entity hints dict organised by category.

    Returns:
        A fully populated BulletinGeneratorInput dataclass instance.
    """
    channel_id = resolve_channel_id(session_key)
    visibility = derive_visibility(session_key)

    # Use first contact ID for scope derivation if available
    primary_contact = contact_ids[0] if contact_ids else None
    scope = derive_scope(session_key, contact_id=primary_contact)
    channel_type = derive_channel_type(session_key)

    start_dt = datetime.fromisoformat(transcript_start)
    end_dt = datetime.fromisoformat(transcript_end)

    # Build a stable transcript_range_id from session_key + start time
    range_slug = start_dt.strftime("%Y%m%d%H%M")
    transcript_range_id = f"range-{range_slug}"

    return BulletinGeneratorInput(
        session_id=session_key,
        transcript_range_id=transcript_range_id,
        transcript_start_time=start_dt,
        transcript_end_time=end_dt,
        channel_id=channel_id,
        channel_type=channel_type,
        source_type="session_transcript_range",
        actor_contact_id=primary_contact,
        visibility=visibility,
        scope=scope,
        known_entities=known_entities or {},
        transcript=transcript_text,
    )
