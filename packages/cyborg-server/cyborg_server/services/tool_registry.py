"""Tool registry — centralized tool assembly for LLM dispatch.

Instead of each dispatch site (WhatsApp bridge, email polling, voice) importing
and concatenating tool modules independently, this module provides a single
build_common_tools() that assembles the shared tool set. Channel-specific tools
(outreach, email_reply, send_whatsapp_message) are added by the caller.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cyborg_server.services.tools import Tool
from cyborg_server.services.workspace_tools import make_workspace_tools
from cyborg_server.services.memory_tools import make_memory_tools
from cyborg_server.services.docs_tools import make_docs_tools
from cyborg_server.services.changelog_tools import make_changelog_tools
from cyborg_server.services.email_tools import make_email_send_tools, make_email_thread_tools
from cyborg_server.services.contact_tools import make_contact_tools
from cyborg_server.services.phone_tools import make_phone_tools
from cyborg_server.services.reflection_service import make_reflection_tools
from cyborg_server.services.subagent_tools import make_subagent_tools
from cyborg_server.services.session_tools import make_session_tools
from cyborg_server.services.routine_tools import make_routine_tools

if TYPE_CHECKING:
    from cyborg_server.context import AppContext

logger = logging.getLogger(__name__)


def build_common_tools(
    ctx: AppContext,
    *,
    session_key: str,
    is_trusted: bool = False,
    contact_id: str | None = None,
) -> list[Tool]:
    """Build the standard tool set shared across dispatch channels.

    Returns core tools (workspace, memory, docs, changelog, email_send)
    plus conditional tools based on trust level and config (contact, phone,
    reflection, delegation). Deduplicates by tool name.
    """
    tools: list[Tool] = []
    seen: set[str] = set()

    def _extend(new: list[Tool]) -> None:
        for t in new:
            if t.name not in seen:
                tools.append(t)
                seen.add(t.name)

    # Core tools — available in every session
    _extend(make_workspace_tools(ctx, session_key=session_key))
    _extend(make_memory_tools(ctx, session_key=session_key))
    _extend(make_docs_tools(ctx, session_key=session_key))
    _extend(make_changelog_tools(ctx, session_key=session_key))
    _extend(make_email_send_tools(ctx, session_key=session_key))
    _extend(make_email_thread_tools(ctx, contact_id=contact_id, is_trusted=is_trusted))
    _extend(make_session_tools(ctx, caller_session_key=session_key, is_trusted=is_trusted, contact_id=contact_id))
    _extend(make_routine_tools(ctx, session_key=session_key))

    # Trust-escalated tools
    if is_trusted:
        _extend(make_contact_tools(ctx))
        _extend(make_reflection_tools(ctx, session_key))
        if ctx.settings.harness.skill_dev_enabled:
            _extend(make_subagent_tools(ctx, session_key))

    # Phone subsystem — adds contact + phone tools when enabled
    if ctx.settings.phone.enabled:
        _extend(make_contact_tools(ctx))
        _extend(make_phone_tools(ctx, session_key=session_key))

    return tools
