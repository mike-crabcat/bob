"""Conversation wake service (Bob3 Phase V).

The single channel-agnostic path for waking a conversation with new context:
goal completions, subagent results, thread/call results, deadline wakeups.
Replaces the per-channel relay modules (thread_result_service and friends).

Mechanics: the content is stored as an undispatched user message (so a crash
before dispatch is recovered by the startup sweep), then a turn is dispatched
through the channel's hardened pipeline. WhatsApp rides the bridge's inbound
dispatch spec (attention coordinator, turn claims, effects sends,
delivered-only history). Other channels get a generic workspace-tools turn.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from bob_server.context import AppContext

logger = logging.getLogger(__name__)

# Detached wake dispatches. Holding strong references prevents the asyncio
# scheduler from garbage-collecting a task mid-flight (asyncio keeps only
# weak refs) — a collected task dies silently, dropping the wake. Same
# pattern as the memory service's _remember_tasks.
_pending_dispatches: set[asyncio.Task] = set()


def session_key_to_chat_id(session_key: str) -> str | None:
    """Derive a WhatsApp chat_id (JID) from a session key."""
    parts = session_key.split(":")
    if len(parts) < 5 or parts[2] != "whatsapp":
        return None
    kind, ident = parts[3], parts[4]
    if kind == "dm":
        return f"{ident}@s.whatsapp.net"
    if kind == "group":
        return f"{ident}@g.us"
    return None


async def conversation_channel(ctx: AppContext, conversation_id: str) -> tuple[str, str]:
    """Resolve (channel, channel_session_key) for a conversation.

    For unmerged conversations id == binding session_key so key parsing wins.
    After a merge the survivor id may not match a channel shape — fall back
    to the binding map and prefer a WhatsApp binding (richest wake pipeline),
    then any binding. This is the plan's "key-parsing replaced by binding
    lookups" seam (Phase VI item 3) for the outbound side.
    """
    channel = _channel_of(conversation_id)
    if channel not in ("internal",):
        return channel, conversation_id
    try:
        from bob_server.repositories.conversations import ConversationRepository
        bindings = await ConversationRepository(ctx.db).bindings_for(conversation_id)
    except Exception:
        return channel, conversation_id
    if not bindings:
        return channel, conversation_id
    preferred = next((b for b in bindings if b["channel"] == "whatsapp"), bindings[0])
    return _channel_of(preferred["session_key"]), preferred["session_key"]


async def wake_conversation(
    ctx: AppContext,
    session_key: str,
    content: str,
    *,
    call_category: str = "wakeup",
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Wake ``session_key`` with ``content`` as new context and run a turn.

    Returns True when a dispatch was armed, False when the content was stored
    but no dispatcher was available (it stays undispatched for recovery).
    """
    from bob_server.services.session_service import SessionService

    channel, _ = await conversation_channel(ctx, session_key)
    await SessionService(ctx).add_message(
        session_key, "user", content,
        channel=channel, metadata=metadata, dispatched=0,
        provenance="wake_nudge",
    )

    if channel == "whatsapp":
        bridge = getattr(ctx, "whatsapp_bridge", None)
        if bridge is not None:
            try:
                await bridge.wake_session(session_key)
                return True
            except Exception:
                logger.exception("wake: WhatsApp dispatch failed for %s", session_key)
                return False
        logger.warning("wake: no WhatsApp bridge; %s stored undispatched", session_key)
        return False

    return await _generic_wake_dispatch(ctx, session_key, content, call_category)


def _channel_of(session_key: str) -> str:
    parts = session_key.split(":")
    if len(parts) >= 3 and parts[0] == "agent":
        return parts[2]
    if session_key.startswith("subagent:"):
        return "subagent"
    return "internal"


async def _generic_wake_dispatch(
    ctx: AppContext,
    session_key: str,
    content: str,
    call_category: str,
) -> bool:
    """Fallback turn for non-WhatsApp conversations: workspace tools only,
    assistant output stored to history (mirrors the old thread_result
    behaviour for non-WA origins)."""
    import asyncio
    from uuid import uuid4

    from bob_server.services.llm_dispatch import LLMDispatchService
    from bob_server.services.prompt_assembler import build_chat_messages, load_workspace_prompt
    from bob_server.services.session_service import SessionService
    from bob_server.services.workspace_tools import make_workspace_tools

    settings = ctx.settings
    if not settings.openai.enabled:
        return False

    tools = make_workspace_tools(ctx, session_key=session_key)
    # Bob Events §1.5: goal tools on the generic wake path — a goal_deadline
    # wake tells the LLM to "revise the goal", so it must actually be able to.
    # The wake is system-initiated, so no separate trust gate applies.
    from bob_server.services.goal_tools import make_goal_tools
    tools.extend(make_goal_tools(ctx, session_key))
    from bob_server.services.approval_tools import make_approval_tools
    tools.extend(make_approval_tools(ctx, session_key))
    dispatch_id = str(uuid4())

    async def _run() -> None:
        try:
            workspace_prompt = await load_workspace_prompt(
                settings.harness.workspace_dir, db=ctx.db)
            from bob_server.services.context_assembler import ContextAssembler
            goals_prompt = await ContextAssembler(ctx).goals_block(session_key)
            system_content = "\n\n".join(
                p for p in (workspace_prompt, goals_prompt) if p)
            messages = await build_chat_messages(
                content, session_key, db=ctx.db,
                system_content=system_content, max_history=20,
            )
            result = await LLMDispatchService(ctx).chat_with_tools(
                messages, tools,
                call_category=call_category,
                session_key=session_key,
                dispatch_id=dispatch_id,
            )
            from bob_server.services.session_service import SessionService
            await SessionService(ctx).mark_dispatched(session_key)
            if result.strip():
                await SessionService(ctx).add_message(
                    session_key, "assistant", result, dispatch_id=dispatch_id)
        except Exception:
            logger.exception("wake: generic dispatch failed for %s", session_key)

    task = asyncio.create_task(_run())
    _pending_dispatches.add(task)
    task.add_done_callback(_pending_dispatches.discard)
    return True
