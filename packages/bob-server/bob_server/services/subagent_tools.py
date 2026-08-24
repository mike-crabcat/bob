"""Subagent tools — let Bob's LLM manage async subagents."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from bob_server.services.tools import tool

if TYPE_CHECKING:
    from bob_server.context import AppContext

logger = logging.getLogger(__name__)


def make_subagent_tools(ctx: AppContext, session_key: str) -> list:
    """Create subagent management tools for a trusted session."""

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
        - 'script': runs `task` as a literal bash command in the workspace
          (same sandbox/env as the bash tool) WITHOUT blocking this conversation.
          USE THIS for any skill script expected to take more than ~10 seconds —
          image generation (openai-image), browser automation, PDF rendering,
          video/audio processing. Flow: (1) send the user a short ack message
          FIRST ("On it — image coming shortly"), (2) create_subagent(
          task="python skills/openai-image/openai_image.py --prompt '...' --output
          /home/bob/workspace/generated-images/car.png", agent_type='script'),
          (3) END YOUR TURN — do NOT poll check_subagent. When the script
          finishes you will be woken automatically with the output; send the
          artifact to the user then. Never run slow scripts with the bash tool —
          it freezes the whole conversation while they run.
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
        from bob_server.services.subagent_service import SubagentService

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
    async def check_subagent(subagent_id: str) -> str:
        """Check the status and result of a subagent. Returns current status and result if available."""
        from bob_server.services.subagent_service import SubagentService

        svc = SubagentService(ctx)
        result = await svc.check_subagent(subagent_id)
        return json.dumps(result)

    @tool
    async def message_subagent(subagent_id: str, message: str) -> str:
        """Send a follow-up message to a subagent. The subagent will process your message
        and return a response. Only use on subagents in 'waiting_for_parent' status."""
        from bob_server.services.subagent_service import SubagentService

        svc = SubagentService(ctx)
        result = await svc.message_subagent(subagent_id, message)
        return json.dumps(result)

    @tool
    async def list_subagents(status: str = "") -> str:
        """List your subagents, optionally filtered by status.
        Valid statuses: created, running, waiting_for_parent, completed, failed, killed."""
        from bob_server.services.subagent_service import SubagentService

        svc = SubagentService(ctx)
        results = await svc.list_subagents(session_key, status)
        return json.dumps(results)

    @tool
    async def kill_subagent(subagent_id: str) -> str:
        """Kill a running subagent. Cancels execution and marks it as killed."""
        from bob_server.services.subagent_service import SubagentService

        svc = SubagentService(ctx)
        result = await svc.kill_subagent(subagent_id)
        return json.dumps(result)

    return [create_subagent, check_subagent, message_subagent, list_subagents, kill_subagent]
