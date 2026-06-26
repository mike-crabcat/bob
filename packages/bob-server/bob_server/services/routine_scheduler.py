"""RoutineSchedulerTask — fires due routines via the heartbeat loop."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC
from uuid import uuid4

from bob_server.services.routine_service import RoutineService

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


class RoutineSchedulerTask:
    """Checks for due routines and dispatches them as independent async tasks."""

    name = "routine_scheduler"

    async def run(self, ctx) -> None:  # type: ignore[override]
        from bob_server.cron import next_cron_occurrence

        svc = RoutineService(ctx)
        due = await svc.get_due_routines()

        for routine in due:
            # Claim before dispatch: advance next_run_at atomically so the next
            # heartbeat tick no longer sees this routine as due. Without this,
            # a 60s heartbeat can fire the same slow routine twice (the original
            # mark_run only bumped next_run_at after the LLM finished ~30s later).
            next_at = next_cron_occurrence(
                routine["schedule"], timezone=routine.get("timezone")
            ).astimezone(UTC).isoformat()
            if await svc.claim(routine["id"], next_at):
                asyncio.create_task(self._fire_routine(ctx, routine))

    async def _fire_routine(self, ctx, routine: dict) -> None:
        from bob_server.services.session_service import SessionService
        from bob_server.services.prompt_assembler import load_workspace_prompt, build_chat_messages
        from bob_server.services.llm_dispatch import LLMDispatchService
        from bob_server.services.tool_registry import build_common_tools
        from bob_server.services.tools import Tool
        from bob_server.services.routine_service import _format_routine_now

        session_key = routine["session_key"]
        prompt = routine["prompt"]
        name = routine["name"]

        try:
            prompt = f"{_format_routine_now(routine)}\n\n{prompt}"

            session_svc = SessionService(ctx)
            await session_svc.add_message(session_key, "user", prompt, channel="routine")

            settings = ctx.settings
            workspace_prompt = await load_workspace_prompt(settings.harness.workspace_dir, db=ctx.db)

            # Resolve session trust level for correct tool set
            route = await ctx.db.fetch_one(
                "SELECT channel, kind, contact_id FROM session_routes WHERE session_key = ?",
                (session_key,),
            )
            is_trusted = False
            contact_id = route["contact_id"] if route else None
            if route and contact_id:
                contact = await ctx.db.fetch_one(
                    "SELECT is_trusted FROM contacts WHERE id = ? AND deleted_at IS NULL",
                    (contact_id,),
                )
                if contact:
                    is_trusted = bool(contact.get("is_trusted", 0))

            # Routines carry their own self-contained prompt — skip session history
            # (which includes the original "set up this routine" conversation)
            messages = await build_chat_messages(
                prompt, "",
                system_content=workspace_prompt,
            )
            tools = build_common_tools(
                ctx, session_key=session_key, is_trusted=is_trusted,
                contact_id=contact_id, include_routines=False,
            )

            # Add channel-specific delivery tools
            wa_bridge = ctx.whatsapp_bridge
            chat_id = _session_key_to_chat_id(session_key)
            if chat_id and wa_bridge and wa_bridge.connected:
                async def _send_whatsapp_message(text: str) -> str:
                    if text.strip().upper() == "NO_REPLY":
                        return "No reply sent."
                    request_id = await wa_bridge.send_message(chat_id, text)
                    return f"Message sent (request_id={request_id})"

                tools.append(Tool(
                    name="send_whatsapp_message",
                    description=(
                        "Send a reply to the current WhatsApp conversation. "
                        "You MUST call this tool to deliver your response — your text output will NOT be sent."
                    ),
                    parameters={
                        "text": {"type": "string", "description": "The message text to send."},
                    },
                    required=["text"],
                    handler=_send_whatsapp_message,
                ))

            dispatch_id = str(uuid4())
            response = await LLMDispatchService(ctx).chat_with_tools(
                messages, tools,
                call_category="routine",
                session_key=session_key,
                dispatch_id=dispatch_id,
            )

            await session_svc.add_message(session_key, "assistant", response, channel="routine", dispatch_id=dispatch_id)

            # next_run_at was already advanced by claim() before dispatch;
            # just record when this run completed.
            svc = RoutineService(ctx)
            await svc.mark_run(routine["id"])

            logger.info("Routine '%s' fired for session %s", name, session_key)
        except Exception:
            logger.exception("Routine '%s' failed for session %s", name, session_key)
