"""Shared result dispatch for thread-oriented tasks (phone calls, email threads).

When a phone call or email thread completes, this module dispatches the result
back to the originating session (e.g., a WhatsApp group) so the agent can relay it.
Follows the pattern established by finish_outreach in whatsapp_outreach_tools.py.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING
from uuid import uuid4

from bob_server.context import AppContext
from bob_server.services.session_service import SessionService
from bob_server.services.llm_dispatch import LLMDispatchService
from bob_server.services.prompt_assembler import load_workspace_prompt, build_chat_messages
from bob_server.services.workspace_tools import make_workspace_tools
from bob_server.services.tools import Tool

if TYPE_CHECKING:
    from bob_server.services.whatsapp_bridge_service import WhatsAppBridgeService

logger = logging.getLogger(__name__)


def _session_key_to_chat_id(session_key: str) -> str | None:
    """Derive a WhatsApp chat_id (JID) from a session key."""
    parts = session_key.split(":")
    if len(parts) < 5 or parts[2] != "whatsapp":
        return None
    kind = parts[3]
    ident = parts[4]
    if kind == "dm":
        return f"{ident}@s.whatsapp.net"
    if kind == "group":
        return f"{ident}@g.us"
    return None


async def dispatch_thread_result(
    ctx: AppContext,
    *,
    origin_session_key: str,
    result_content: str,
    call_category: str,
    wa_service: WhatsAppBridgeService | None = None,
) -> None:
    """Dispatch a thread result back to the originating session.

    Stores the result as a user message in the origin session, then dispatches
    an LLM call with appropriate send tools so the origin agent can relay it.
    """
    db = ctx.db
    session_svc = SessionService(ctx)

    await session_svc.add_message(
        origin_session_key, "user", result_content,
        channel="whatsapp",
    )

    origin_chat_id = _session_key_to_chat_id(origin_session_key)
    settings = ctx.settings

    tools = make_workspace_tools(ctx, session_key=origin_session_key)
    message_was_sent = [False]

    if origin_chat_id and wa_service and wa_service.connected:
        async def _send_whatsapp_message(text: str) -> str:
            message_was_sent[0] = True
            if text.strip().upper() == "NO_REPLY":
                return "No reply sent."
            await wa_service.send_message(origin_chat_id, text)
            return "Message sent"

        tools.append(Tool(
            name="send_whatsapp_message",
            description=(
                "Send a reply to the current WhatsApp conversation. "
                "You MUST call this tool to deliver your response — your text output will NOT be sent."
            ),
            parameters={"text": {"type": "string", "description": "The message text to send."}},
            required=["text"],
            handler=_send_whatsapp_message,
        ))

    dispatch_id = str(uuid4())

    async def _run_dispatch() -> str:
        from bob_server.services.session_agenda_service import SessionAgendaService
        from bob_server.services.tap import tap_dispatch, tap_enabled

        agenda_svc = SessionAgendaService(ctx)
        origin_agenda = await agenda_svc.get_effective_agenda(
            origin_session_key, "whatsapp",
        )

        workspace_prompt = await load_workspace_prompt(settings.harness.workspace_dir, db=db)
        system_content = "\n\n".join(
            p for p in (workspace_prompt, origin_agenda) if p
        )

        messages = await build_chat_messages(
            result_content,
            origin_session_key,
            db=db,
            system_content=system_content,
            max_history=20,
        )

        llm_result = await LLMDispatchService(ctx).chat_with_tools(
            messages, tools,
            call_category=call_category,
            session_key=origin_session_key,
            dispatch_id=dispatch_id,
        )

        if not message_was_sent[0] and llm_result.strip() and origin_chat_id and wa_service:
            if tap_enabled():
                llm_result = await tap_dispatch(
                    ctx, messages=messages, tools=tools,
                    session_key=origin_session_key,
                    send_tool_name="send_whatsapp_message",
                    first_result=llm_result,
                    call_category=call_category,
                    dispatch_id=dispatch_id,
                )

        await session_svc.add_message(
            origin_session_key, "assistant", llm_result,
            channel="whatsapp",
        )

        return llm_result

    asyncio.create_task(_run_dispatch())

    logger.info(
        "Dispatched thread result to origin session %s (category=%s)",
        origin_session_key, call_category,
    )
