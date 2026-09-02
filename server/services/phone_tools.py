"""Phone call tools — status queries for active and historical calls.

Outbound calls are now placed via ``create_subagent(agent_type="openai_voice")``
in ``subagent_tools.py``, which routes through the OpenAI Realtime bridge. The
older ``make_phone_call`` (default STT→LLM→TTS engine) and ``place_realtime_call``
(duplicate of the subagent path) entry points were removed when the voice
subagent became the canonical dispatch surface.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from server.services.tools import tool

if TYPE_CHECKING:
    from server.context import AppContext

logger = logging.getLogger(__name__)


def make_phone_tools(
    ctx: "AppContext",
    *,
    session_key: str | None = None,
) -> list:
    """Create phone-call status tools for the LLM agent."""

    @tool
    async def get_call_status(call_id: str) -> str:
        """Check the status of a phone call. Returns current status, duration, and exchange count."""
        db = ctx.db
        from server.repositories.phone_calls import PhoneCallRepository
        call = await PhoneCallRepository(db).get(call_id)
        if not call:
            return json.dumps({"ok": False, "error": "Call not found"})
        keys = ("id", "call_sid", "phone_number", "direction", "status", "agenda",
                "exchange_count", "duration_seconds", "started_at", "completed_at")
        return json.dumps({"ok": True, **{k: call.get(k) for k in keys}})

    return [get_call_status]
