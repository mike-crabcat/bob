"""Subagent tools — let Bob's LLM manage async subagents, plus
run_bg_process: supervised background shell commands (process
supervision deliberately NOT modelled as a subagent — 2026-09-20, after
four prose-briefs-into-bash spawn failures came from the confusion; the
script agent_type was retired the same day and its engine folded into the
bg machinery: one process mechanism, one advertised surface)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from server.services.tools import tool

if TYPE_CHECKING:
    from server.context import AppContext

logger = logging.getLogger(__name__)


def make_subagent_tools(ctx: AppContext, session_key: str, *, is_trusted: bool = True) -> list:
    """Create subagent management tools for a session.

    Trusted sessions get the full toolbox. Untrusted sessions (group chats,
    untrusted DM contacts) get run_bg_process — supervised background shell
    commands in the same workspace sandbox as the bash tool the session
    already has (with a mandatory RuntimeMaxSec TTL) — while LLM-loop
    (claude/local) and phone (openai_voice) subagents spend tokens or place
    real calls, so create_subagent refuses every agent type when untrusted,
    and message_subagent (which drives further LLM runs) is withheld
    entirely.
    """

    @tool
    async def create_subagent(
        task: str,
        agent_type: str = "claude",
        persona: bool = False,
        model: str = "",
        contact_id: str | None = None,
        modality: str = "phone",
        goal_parent_id: str = "",
    ) -> str:
        """Spawn a subagent to work on a task asynchronously. Returns subagent_id immediately.

        agent_type:
        - 'claude' (default): spawns Claude CLI subprocess with the task as prompt.
        - 'local': runs in-process via chat_with_tools (faster, no subprocess).
        (For background shell commands use run_bg_process — that is process
        supervision, not a subagent: no model, no judgment, just a command
        whose completion wakes this conversation.)
        - 'openai_voice': places a real voice call to a contact. `task` is a FACTUAL
          BRIEF, not a script: what to find out or achieve, plus constraints (budget,
          dates, what to avoid) — under ~80 words, e.g. "Ask if they have a Sega
          Mega Drive II (original style, not mini) in stock, price/condition, and
          whether they can hold it today. Under $1000. Don't pay or commit."
          Do NOT write greeting lines, "introduce yourself as…", staging, or
          how-to-report instructions — the voice agent owns all of that and has its
          own phone-manner rules; scripted goals get recited verbatim and sound
          robotic. `contact_id` is REQUIRED — look up the contact first with a
          contact search tool, or create one on the spot with
          create_contact(name, phone_number) when the number isn't saved yet
          (e.g. a shop you just looked up).

        modality — use EXACTLY 'phone' or 'voice_link', nothing else:
        - 'phone' (default): rings their actual phone via Twilio. Use this whenever
          the user asks to CALL, PHONE, RING, or DIAL someone — an actual phone call
          is what those words mean.
        - 'voice_link': the contact gets a browser voice-session URL instead. Only
          use when the user wants a link/chat call or has no phone number. The
          response includes `voice_url` — YOU must then send it to the contact via
          send_whatsapp_message (with a friendly intro); the call starts when they
          tap it.

        The subagent stays in 'running' until the call ends; the transcript
        lands in `result` via check_subagent.

        persona: if true and local, load full agent persona; if false, uses minimal system prompt.
        model: override model for local subagents (default: gpt-5.6-sol).

        After calling this, you MUST send a message to the user summarizing what you delegated.
        Use check_subagent to poll for results and message_subagent for follow-up."""
        from server.services.subagent_service import SubagentService

        if not is_trusted:
            # Fail before the service's alias normalisation can coerce an
            # invented type: untrusted input must never reach the LLM-loop
            # or phone-call paths. Background commands are the one thing
            # untrusted sessions keep — via run_bg_process, TTL-capped.
            return json.dumps({
                "ok": False,
                "error": (
                    "This conversation is untrusted: subagents are not "
                    "available here. For a background command use "
                    "run_bg_process(command=...) — same workspace sandbox "
                    "as your bash tool (with a run-time cap)."
                ),
            })

        svc = SubagentService(ctx)
        result = await svc.create_subagent(
            task,
            session_key,
            agent_type=agent_type,
            goal_parent_id=goal_parent_id or None,
            persona=persona,
            model=model,
            contact_id=contact_id,
            modality=modality,
        )
        return json.dumps(result)

    @tool
    async def run_bg_process(command: str) -> str:
        """Run a shell COMMAND as a supervised background process and return
        its handle immediately. NOT a subagent — no model, no judgment, no
        briefs: `command` is literal bash in the workspace sandbox (same env
        as your bash tool) that outlives this turn with NO wall-clock cap
        and SURVIVES bob-server restarts. THE tool for any script expected
        to take more than ~10 seconds — image generation, browser
        automation, PDF rendering, indexing, data imports, long downloads.
        When the process exits you are woken automatically with its exit
        code and log tail; send any artifact to the user then. Flow:
        (1) send the user a short ack FIRST ("On it — image coming shortly"),
        (2) run_bg_process(command="python skills/openai-image/openai_image.py \
--prompt '...' --output /home/bob/workspace/generated-images/car.png"),
        (3) END YOUR TURN — do NOT poll (bg_logs <name> if you must peek).
        If you find yourself writing a PROSE BRIEF here, stop: you want
        create_subagent(agent_type='claude'). Never run slow commands with
        the bash tool — it freezes the whole conversation."""
        from server.services.process_tools import start_bg_job

        result = await start_bg_job(
            ctx, session_key, command, is_trusted=is_trusted)
        return json.dumps(result)

    @tool
    async def check_subagent(subagent_id: str) -> str:
        """Check the status and result of a subagent. Returns current status and result if available."""
        from server.services.subagent_service import SubagentService

        svc = SubagentService(ctx)
        result = await svc.check_subagent(subagent_id, parent_session_key=session_key)
        return json.dumps(result)

    @tool
    async def message_subagent(subagent_id: str, message: str) -> str:
        """Send a follow-up message to a subagent. The subagent will process your message
        and return a response. Only use on subagents in 'waiting_for_parent' status."""
        from server.services.subagent_service import SubagentService

        svc = SubagentService(ctx)
        result = await svc.message_subagent(subagent_id, message, parent_session_key=session_key)
        return json.dumps(result)

    @tool
    async def list_subagents(status: str = "") -> str:
        """List your subagents, optionally filtered by status.
        Valid statuses: created, running, waiting_for_parent, completed, failed, killed."""
        from server.services.subagent_service import SubagentService

        svc = SubagentService(ctx)
        results = await svc.list_subagents(session_key, status)
        return json.dumps(results)

    @tool
    async def kill_subagent(subagent_id: str) -> str:
        """Kill a running subagent. Cancels execution and marks it as killed."""
        from server.services.subagent_service import SubagentService

        svc = SubagentService(ctx)
        result = await svc.kill_subagent(subagent_id, parent_session_key=session_key)
        return json.dumps(result)

    tools = [create_subagent, run_bg_process, check_subagent,
             list_subagents, kill_subagent]
    if is_trusted:
        tools.append(message_subagent)
    return tools
