"""Tap — follow-up LLM dispatch when the agent didn't use its send tool.

Instead of auto-sending raw LLM output, a tap gives the agent a second
chance: a lightweight follow-up call reminding it that it has a send
tool available. If it uses it, great. If not, we accept the decision.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from bob_server.services.tools import Tool

if TYPE_CHECKING:
    from bob_server.context import AppContext

logger = logging.getLogger(__name__)


def tap_enabled() -> bool:
    import os
    return os.environ.get("BOB_ENABLE_TAP", "").lower() in ("1", "true", "yes")


async def tap_dispatch(
    ctx: AppContext,
    *,
    messages: list[dict],
    tools: list[Tool],
    session_key: str,
    send_tool_name: str,
    first_result: str,
    call_category: str,
    dispatch_id: str,
    contact_id: str | None = None,
) -> str:
    """Follow-up dispatch when the LLM didn't use its send tool.

    Appends the first result as an assistant message and a reminder as
    a user message, then dispatches again with the same tools.
    Returns the final result (which may or may not have used the send tool).
    """
    from bob_server.services.llm_dispatch import LLMDispatchService

    tap_messages = messages + [
        {"role": "assistant", "content": first_result},
        {"role": "user", "content": (
            f"You just generated this response but did not call {send_tool_name} to send it:\n\n"
            f"---\n{first_result}\n---\n\n"
            f"Call {send_tool_name} now to deliver it. This is not optional — "
            f"your response will NOT reach the user unless you call this tool."
        )},
    ]

    logger.info(
        "Tap dispatch: reminding agent to use %s (session=%s, category=%s)",
        send_tool_name, session_key, call_category,
    )

    result = await LLMDispatchService(ctx).chat_with_tools(
        tap_messages,
        tools,
        call_category=f"{call_category}_tap",
        session_key=session_key,
        dispatch_id=f"{dispatch_id}_tap",
        contact_id=contact_id,
    )

    return result
