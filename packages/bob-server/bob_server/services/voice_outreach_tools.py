"""Voice outreach tools — let Bob offer a browser voice call from a chat.

Currently one tool: ``initiate_voice_call``. Built per-dispatch inside the
WhatsApp handler (so the bridge service is in scope), like the other outreach
tool factories. DM-only for v1 — groups raise "who's talking?".
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

# Modality aliases — accept the vocabulary people actually use. "voip" / "realtime"
# / "browser" / "app" all mean the no-phone-call browser voice link; "phone" / "call"
# / "dial" / "twilio" mean an actual outbound call.
_VOICE_LINK_ALIASES = {
    "voice_link", "voip", "realtime", "browser", "link", "app", "web", "data",
    "whatsapp", "session",
}
_PHONE_ALIASES = {
    "phone", "call", "twilio", "dial", "telephone", "cell", "landline", "sms",
}


def _normalise_modality(modality: str) -> str | None:
    m = (modality or "voice_link").strip().lower()
    if m in _VOICE_LINK_ALIASES:
        return "voice_link"
    if m in _PHONE_ALIASES:
        return "phone"
    return None


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

    @tool
    async def reach_out_with_voice_call(
        contact_id: str,
        goal: str,
        modality: str = "voice_link",
        intro_message: str = "",
    ) -> str:
        """Reach out to a contact by voice to achieve a specific goal. Reports the
        outcome back to this conversation when the call ends.

        modality (default "voice_link" — omit it unless you specifically want a phone call):
        - "voice_link" (also: voip, realtime, browser, app, web, whatsapp): DM the
          contact a browser voice session link. No phone call, no app install, free.
          This is the right default for WhatsApp contacts and for "VoIP"/"realtime" requests.
        - "phone" (also: call, dial, twilio, telephone): place an actual outbound call
          to their number. Costs money and rings their phone — confirm with the user
          first unless they explicitly said "call"/"phone".

        goal = what the voice agent should find out or achieve, phrased as a task
        (e.g. "find out when Alice is free next week", "confirm the delivery arrived").
        intro_message = opening line for the voice_link DM (default: a friendly invite).

        Prefer this tool over place_realtime_call — it covers both modalities and
        reports the outcome back to this conversation. Use place_realtime_call only
        when you specifically need the phone path with custom raw instructions.
        """
        from bob_server.services.voice_session_service import VoiceSessionService

        contact = await ctx.db.fetch_one(
            "SELECT id, name, phone_number FROM contacts WHERE id = ? AND deleted_at IS NULL",
            (contact_id,),
        )
        if contact is None:
            return json.dumps({"ok": False, "error": "Contact not found"})
        phone = contact["phone_number"]
        if not phone:
            return json.dumps({"ok": False, "error": "Contact has no phone number"})

        modality = _normalise_modality(modality)
        if modality is None:
            return json.dumps({
                "ok": False,
                "error": f"unknown modality. Use 'voice_link' (browser, default) or 'phone' (Twilio call).",
            })

        if modality == "voice_link":
            if not (wa_service and wa_service.connected):
                return json.dumps({"ok": False, "error": "WhatsApp bridge not connected"})

            import re
            phone_digits = re.sub(r"\D", "", phone)
            target_session = f"agent:main:whatsapp:dm:{phone_digits}"
            jid = f"{phone_digits}@s.whatsapp.net"

            svc = VoiceSessionService(ctx)
            created = await svc.create(
                origin_session_key=target_session,
                voice=ctx.settings.openai_realtime.voice,
                goal=goal,
                report_back_session_key=session_key,
            )
            url = created["url"]
            intro = intro_message.strip() or f"Hi {contact['name']}, it's Bob — could we hop on a quick voice call?"
            try:
                await wa_service.send_message(jid, f"{intro}\n{url}")
            except Exception as e:
                logger.warning("reach_out voice_link send failed to %s: %s", jid, e)
                return json.dumps({"ok": False, "error": f"failed to send DM: {e}"})

            logger.info("reach_out voice_link -> %s (goal=%s)", contact["name"], goal[:60])
            return json.dumps({
                "ok": True,
                "modality": "voice_link",
                "contact": contact["name"],
                "url": url,
            })

        # modality == "phone"
        phone_settings = ctx.settings.phone
        if not phone_settings.enabled:
            return json.dumps({"ok": False, "error": "Phone subsystem is not enabled"})

        instructions = (
            f"You are calling {contact['name']}.\n\n"
            f"Goal: {goal}\n\n"
            f"Be polite and brief. When you have the answer (or it's clear you can't "
            f"get it), call report_success with a one-line summary and the key facts "
            f"in details, or report_failure with the reason. Then call end_call."
        )
        from bob_server.routers.phone import initiate_outbound_call
        result = await initiate_outbound_call(
            db=ctx.db,
            settings=ctx.settings,
            phone_settings=phone_settings,
            to_number=phone,
            agenda=goal,
            app_state=None,
            origin_session_key=session_key,
            engine="openai_realtime",
            realtime_meta={"instructions": instructions, "voice": ""},
        )
        if "error" in result:
            return json.dumps({"ok": False, "error": result["error"]})

        logger.info("reach_out phone -> %s (goal=%s)", contact["name"], goal[:60])
        return json.dumps({
            "ok": True,
            "modality": "phone",
            "contact": contact["name"],
            "call_id": result.get("call_id"),
            "call_sid": result.get("call_sid"),
        })

    return [initiate_voice_call, reach_out_with_voice_call]
