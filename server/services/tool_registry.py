"""Tool registry — centralized tool assembly for LLM dispatch.

Instead of each dispatch site (WhatsApp bridge, email polling, voice) importing
and concatenating tool modules independently, this module provides a single
build_common_tools() that assembles the shared tool set. Channel-specific tools
(outreach, email_reply, send_whatsapp_message) are added by the caller.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from server.services.tools import Tool
from server.services.workspace_tools import make_workspace_tools
from server.services.process_tools import make_process_tools
from server.services.memory_tools import make_memory_tools
from server.services.docs_tools import make_docs_tools
from server.services.changelog_tools import make_changelog_tools
from server.services.email_tools import make_email_send_tools, make_email_thread_tools
from server.services.contact_tools import make_contact_tools
from server.services.phone_tools import make_phone_tools
from server.services.reflection_service import make_reflection_tools
from server.services.subagent_tools import make_subagent_tools
from server.services.session_tools import make_session_tools
from server.services.routine_tools import make_routine_tools

if TYPE_CHECKING:
    from server.context import AppContext

logger = logging.getLogger(__name__)


def build_common_tools(
    ctx: AppContext,
    *,
    session_key: str,
    is_trusted: bool = False,
    contact_id: str | None = None,
    include_routines: bool = True,
) -> list[Tool]:
    """Build the standard tool set shared across dispatch channels.

    Returns core tools (workspace, memory, docs, changelog, email_send)
    plus conditional tools based on trust level and config (contact, phone,
    reflection, delegation). Deduplicates by tool name.

    Set ``include_routines=False`` for routine dispatch: a routine prompt
    tends to drift toward editing routines (read_routine/write_routine)
    instead of executing the action, so those tools are withheld from the
    routine's own dispatch.
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
    _extend(make_process_tools(ctx))
    _extend(make_memory_tools(ctx, session_key=session_key))
    _extend(make_docs_tools(ctx, session_key=session_key))
    _extend(make_changelog_tools(ctx, session_key=session_key))
    _extend(make_email_send_tools(ctx, session_key=session_key))
    _extend(make_email_thread_tools(ctx, contact_id=contact_id, is_trusted=is_trusted))
    _extend(make_session_tools(
        ctx, is_trusted=is_trusted, contact_id=contact_id, session_key=session_key,
    ))
    if include_routines:
        _extend(make_routine_tools(ctx, session_key=session_key))

    # Dream plan tools — participants adjust plans conversationally (session-bound)
    if ctx.settings.dream.enabled:
        from server.services.dream.tools import make_dream_tools

        _extend(make_dream_tools(ctx, session_key=session_key))

    # Trust-escalated tools
    if is_trusted:
        _extend(make_contact_tools(ctx, is_trusted=True))
        _extend(make_reflection_tools(ctx, session_key))

    # Subagents are the async-execution mechanism the skill index advertises
    # to every session (image gen, browser runs, PDF rendering), so withholding
    # them entirely from untrusted sessions pushes the LLM into blocking bash
    # runs instead. Script subagents run in the same sandbox as the bash tool
    # the session already has — no escalation — so untrusted sessions get
    # script-only create_subagent (enforced in make_subagent_tools);
    # claude/local/openai_voice (LLM spend, phone calls) stay trusted-only.
    if ctx.settings.harness.skill_dev_enabled:
        _extend(make_subagent_tools(ctx, session_key, is_trusted=is_trusted))

    # Phone subsystem — adds contact + phone tools when enabled.
    # create_contact stays trust-gated here too: phone-enabled untrusted
    # sessions get search only.
    if ctx.settings.phone.enabled:
        _extend(make_contact_tools(ctx, is_trusted=is_trusted))
        _extend(make_phone_tools(ctx, session_key=session_key))

    # Home Assistant — adds current_location() when configured
    if ctx.settings.homeassistant.enabled:
        from server.services.location_tools import make_location_tools
        _extend(make_location_tools(ctx))

    return tools
