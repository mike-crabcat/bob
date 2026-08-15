"""Voice outreach tools — let Bob offer a browser voice call from a chat.

One tool: ``initiate_voice_call`` (offer a link in the current DM). Built
per-dispatch inside the WhatsApp handler (so the bridge service is in scope),
like the other outreach tool factories.

Task-oriented voice dispatch (phone or voice_link, to any contact) lives in
``create_subagent(agent_type="openai_voice")`` — see subagent_tools.
``reach_out_with_voice_call`` was retired 2026-08-14: its phone branch
duplicated the subagent path, and its voice_link default + docstring bias
caused the LLM to send links when the user explicitly asked for phone calls.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from bob_server.services.tools import tool

if TYPE_CHECKING:
    from bob_server.context import AppContext
    from bob_server.services.whatsapp_bridge_service import WhatsAppBridgeService

logger = logging.getLogger(__name__)


def make_voice_outreach_tools(
    ctx: "AppContext",
    wa_service: "WhatsAppBridgeService",
    session_key: str,
) -> list:
    """Build the voice outreach tools for a WhatsApp dispatch.

    Only attached in DM contexts (the caller is responsible for not calling
    this in groups).
    """

    @tool
    async def initiate_voice_call(goal: str = "") -> str:
        """Offer the user a voice call. Sends them a link in this chat they can tap
        to start talking with Bob in their browser — no phone call, no app. Use when
        the user asks to talk, or voice would clearly be a better modality than text
        (long back-and-forth, emotional topic). The session shares memory with this
        chat, so Bob knows what you've discussed.

        If the user has a specific task in mind (e.g. "quiz me about dinner",
        "talk me through the plan"), pass it as `goal` so the voice agent knows its
        purpose. Omit goal for an open conversation.

        NOTE: this offers a BROWSER LINK only. If the user wants their actual
        phone to ring, use create_subagent(agent_type="openai_voice",
        modality="phone") instead.
        """
        from bob_server.services.voice_session_service import VoiceSessionService
        from bob_server.services.thread_result_service import _session_key_to_chat_id

        chat_id = _session_key_to_chat_id(session_key)
        if chat_id is None or "@s.whatsapp.net" not in chat_id:
            return json.dumps({"ok": False, "error": "voice calls are DM-only for now"})

        svc = VoiceSessionService(ctx)
        existing = await svc.find_active(session_key)
        if existing:
            url = f"{ctx.settings.resolved_public_url}/voice/session.html?id={existing['id']}"
            return json.dumps({"ok": True, "url": url, "reused": True})

        created = await svc.create(
            session_key,
            voice=ctx.settings.openai_realtime.voice,
            goal=goal,
            phone_number="+" + chat_id.split("@")[0],
        )
        url = created["url"]

        if wa_service and wa_service.connected:
            try:
                await wa_service.send_message(chat_id, f"Tap to talk to me: {url}")
            except Exception as e:
                logger.warning("Failed to send voice link to %s: %s", chat_id, e)
                return json.dumps({"ok": False, "error": f"failed to send link: {e}"})

        logger.info("Voice call offered in session %s (goal=%s)", session_key, bool(goal))
        return json.dumps({"ok": True, "url": url})

    return [initiate_voice_call]
