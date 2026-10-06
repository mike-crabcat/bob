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

from server.context import AppContext

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
        from server.repositories.conversations import ConversationRepository
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
    provenance: str = "wake_nudge",
) -> bool:
    """Wake ``session_key`` with ``content`` as new context and run a turn.

    ``provenance`` labels the stored row: ``steer`` rows (steering requests,
    services/steering.py) carry requester attribution in their content and
    count as a human stimulus for the dispatch runner's detach/silence
    policies; ``task_relay`` rows (background-task results, backburner via
    settle_goal) exist to speak — they stay rescue-eligible when the model
    skips its send call, but never detach (relay detaching amplifies);
    everything else stays a ``wake_nudge``.

    Returns True when a dispatch was armed, False when the content was stored
    but no dispatcher was available (it stays undispatched for recovery).
    """
    from server.services.session_service import SessionService

    channel, _ = await conversation_channel(ctx, session_key)
    await SessionService(ctx).add_message(
        session_key, "user", content,
        channel=channel, metadata=metadata, dispatched=0,
        provenance=provenance,
    )

    if channel == "whatsapp":
        bridge = getattr(ctx, "whatsapp_bridge", None)
        if bridge is not None:
            try:
                # call_category rides along so wake-path turns log as their
                # trigger (steer/routine/…) instead of whatsapp_incoming —
                # a human row in the batch still wins inside wake_session.
                await bridge.wake_session(session_key, call_category=call_category)
                return True
            except Exception:
                logger.exception("wake: WhatsApp dispatch failed for %s", session_key)
                return False
        logger.warning("wake: no WhatsApp bridge; %s stored undispatched", session_key)
        return False

    if channel == "email":
        # Steered/woken email threads run with the thread's REAL toolset
        # (email_reply etc.) — the generic fallback is workspace-only and
        # could never answer in-thread (2026-09-23: email steering).
        from server.services.email_polling_service import EmailPollingService

        try:
            return await EmailPollingService(ctx).wake_thread(
                session_key, content, call_category)
        except Exception:
            logger.exception("wake: email dispatch failed for %s", session_key)
            return False

    return await _generic_wake_dispatch(ctx, session_key, content, call_category)


def _channel_of(session_key: str) -> str:
    parts = session_key.split(":")
    if len(parts) >= 3 and parts[0] == "agent":
        return parts[2]
    if session_key.startswith("subagent:"):
        return "subagent"
    return "internal"



async def _session_tool_principal(
    ctx: AppContext, session_key: str,
) -> tuple[bool, str | None]:
    """(is_trusted, contact_id) for the wake path's session tools. Goal
    rooms inherit their creator's principal (2026-10-01 census gap: the
    un-parameterised call scoped every room to its own history); other
    utilities resolve their binding's contact; anything else is untrusted
    with no contact — own-session reads only."""
    if session_key.startswith("agent:goal-"):
        from server.services.goal_rooms import room_creator_principal
        return await room_creator_principal(ctx, session_key)
    if session_key.startswith("agent:") and session_key.endswith(":utility"):
        from server.repositories.conversations import ConversationRepository
        binding = await ConversationRepository(ctx.db).active_binding(
            session_key)
        contact_id = (binding or {}).get("contact_id")
        if contact_id:
            from server.repositories.contacts import ContactRepository
            c = await ContactRepository(ctx.db).get(contact_id)
            return bool(c and c.get("is_trusted")), contact_id
    return False, None


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

    from server.services.llm_dispatch import LLMDispatchService
    from server.services.prompt_assembler import build_chat_messages, load_workspace_prompt
    from server.services.session_service import SessionService
    from server.services.workspace_tools import make_workspace_tools

    settings = ctx.settings
    if not settings.openai.enabled:
        return False

    # Utility conversations (docs/utility-conversations-plan.md): the charter
    # is the turn's behaviour spec and the model tier defaults to cheap. A
    # utility session with no live spec (killed, disabled, denied) stores but
    # never dispatches — the router already logs those as log-only; this is
    # the defensive second gate. A report_to target adds the send_report
    # alert channel to the toolset.
    charter_block = ""
    utility_model: str | None = None
    utility_tools: list = []
    if session_key.startswith("agent:") and session_key.endswith(":utility"):
        from server.services.utility_conversations import (
            make_report_to_tool, utility_turn_spec,
        )
        spec = await utility_turn_spec(ctx, session_key)
        if spec is None:
            logger.info("wake: utility %s has no live spec — stored "
                        "undispatched", session_key)
            return False
        charter_block, utility_model, report_to = spec
        if report_to:
            utility_tools = make_report_to_tool(ctx, session_key, report_to)
        # Goal rooms (docs/goal-rooms-plan.md): the room-scoped tool surface —
        # state block writes, evidenced close, child spawning, subscription
        # self-management, plus gated DM outreach when the bridge is up.
        # No goal_id juggling: the session IS the room.
        if session_key.startswith("agent:goal-"):
            from server.services.goal_rooms import room_turn_tools
            utility_tools = list(utility_tools) + room_turn_tools(
                ctx, session_key)
            # Creator-scoped capabilities (2026-09-25): the room inherits its
            # creator's principal — roster lookup with the creator's own
            # trust, so a member's goal room sees only what the member
            # could see. NULL creator (system/dream) → untrusted defaults.
            from server.services.goal_rooms import room_creator_principal
            from server.services.group_tools import make_group_lookup_tools
            cr_trusted, cr_contact_id = await room_creator_principal(
                ctx, session_key)
            utility_tools = list(utility_tools) + make_group_lookup_tools(
                ctx, is_trusted=cr_trusted, contact_id=cr_contact_id,
                session_key=session_key)
            # Goal loop (docs/goal-execution-plan.md): the continuation
            # contract + strategies-tree tools, when the loop is enabled.
            from server.services import goal_loop
            if goal_loop.loop_enabled(ctx):
                utility_tools = list(utility_tools) + goal_loop.make_loop_tools(
                    ctx, session_key)

    tools = make_workspace_tools(ctx, session_key=session_key)
    # Session tools (find/search history — the same search_session_messages
    # the chat path carries, 2026-09-18 consolidation: one tool, one name,
    # one habit) + the record-discipline note below.
    from server.services.session_tools import make_session_tools
    # Creator-scoped history reads (2026-10-01 census gap): rooms inherit
    # their creator's principal — same rule as make_group_lookup_tools
    # below — so a trusted creator's room can page foreign groups' history
    # with get_session_messages. The un-parameterised call scoped EVERY
    # utility session to its own history only, which sent the census goal
    # routing through Mike's DM as a workaround (its completer had the
    # tool; the room didn't).
    _st_trusted, _st_contact = await _session_tool_principal(
        ctx, session_key)
    tools.extend(make_session_tools(
        ctx, session_key=session_key,
        is_trusted=_st_trusted, contact_id=_st_contact))
    # Work tools on the generic wake path (rooms, utilities, wakes).
    from server.services.tasks import make_task_tools
    tools.extend(make_task_tools(ctx, session_key))
    from server.services.approval_tools import make_approval_tools
    tools.extend(make_approval_tools(ctx, session_key))
    tools.extend(utility_tools)
    # MCP tools for goal rooms (web search etc.) — scoped by the room's
    # principal like the session tools. Without them a pricing room
    # scraped vendor sites with curl | sed for 16 rounds (2026-10-06).
    mcp_note = ""
    if session_key.startswith("agent:goal-"):
        try:
            from server.services.mcp_service import (
                make_mcp_tools, mcp_transparency_note)
            tools.extend(await make_mcp_tools(
                ctx, session_key=session_key, is_trusted=_st_trusted,
                reserved={t.name for t in tools}))
            mcp_note = await mcp_transparency_note(
                ctx, session_key=session_key, is_trusted=_st_trusted)
        except Exception:
            logger.warning("wake: MCP tools unavailable for %s", session_key,
                           exc_info=True)
    dispatch_id = str(uuid4())

    # Goal loop (docs/goal-execution-plan.md): arm the room turn — event
    # beats timer (pending continuation slot cancelled; any wake supersedes
    # a declared wait) + the before-snapshot the settle-path delta uses.
    loop_token = None
    if session_key.startswith("agent:goal-"):
        try:
            from server.services import goal_loop
            loop_token = await goal_loop.begin_turn(ctx, session_key)
        except Exception:
            logger.exception("goal loop: begin_turn failed for %s", session_key)

    async def _run() -> None:
        try:
            workspace_prompt = await load_workspace_prompt(
                settings.harness.workspace_dir, db=ctx.db)
            from server.services.context_assembler import ContextAssembler
            goals_prompt = await ContextAssembler(ctx).goals_block(session_key)
            from server.services.history_tools import HISTORY_DISCIPLINE_NOTE
            system_content = "\n\n".join(
                p for p in (workspace_prompt, goals_prompt, charter_block,
                            HISTORY_DISCIPLINE_NOTE, mcp_note) if p)
            messages = await build_chat_messages(
                content, session_key, db=ctx.db,
                system_content=system_content, max_history=20,
            )
            result = await LLMDispatchService(ctx).run_turn(
                messages, tools,
                model=utility_model,
                call_category=call_category,
                session_key=session_key,
                dispatch_id=dispatch_id,
            )
            from server.services.session_service import SessionService
            await SessionService(ctx).mark_dispatched(session_key)
            if result.strip():
                await SessionService(ctx).add_message(
                    session_key, "assistant", result, dispatch_id=dispatch_id)
            if loop_token is not None:
                try:
                    from server.services import goal_loop
                    await goal_loop.end_turn(ctx, loop_token, result)
                except Exception:
                    logger.exception("goal loop: end_turn failed for %s",
                                     session_key)
        except Exception:
            logger.exception("wake: generic dispatch failed for %s", session_key)

    task = asyncio.create_task(_run())
    _pending_dispatches.add(task)
    task.add_done_callback(_pending_dispatches.discard)
    return True
