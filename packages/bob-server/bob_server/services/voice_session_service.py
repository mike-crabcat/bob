"""Bob-initiated browser voice sessions — token lifecycle + result dispatch.

A voice_session lets Bob offer the user a voice call from a chat (WhatsApp):
Bob calls the ``initiate_voice_call`` tool, a token is minted, and a link is
sent to the chat. The user taps the link, browser opens, talks to Bob's
realtime voice agent. On hang-up the transcript is summarised and dispatched
back to the originating chat.

The realtime bridge itself is reused (services/realtime_bridge.py); this
service owns the session record, the persona-mode instructions builder, and
the result hand-off.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from bob_server.services.base import BaseService, utcnow

if TYPE_CHECKING:
    from bob_server.services.whatsapp_bridge_service import WhatsAppBridgeService

logger = logging.getLogger(__name__)

_VOICE_PREAMBLE = (
    "You are on a live voice call. Speak naturally and warmly, as if on a phone "
    "call with someone you know.\n\n"
    "CRITICAL: Never think out loud. Never narrate your reasoning, never describe "
    "what you're about to say, never talk through your thought process. Just speak "
    "your final response directly. For example, do NOT say \"let me think about how "
    "to introduce myself\" — just say \"Hi, I'm Bob.\" Do NOT say \"okay, quick "
    "answer\" or \"let me figure this out\" — just answer.\n\n"
    "Keep replies concise and conversational. Do not read out URLs, code, or "
    "markdown. You share memory with the user's chat, so you know who they are and "
    "what you've discussed.\n\n"
    "This is a real back-and-forth conversation. After you speak, STOP and wait "
    "for the other person to reply. A pause after you speak is the other person's "
    "turn to talk. Keep the conversation going naturally; the human will end the "
    "call when they're ready."
)


class VoiceSessionService(BaseService):
    """Create / resolve / complete browser voice sessions tied to a chat session."""

    async def create(
        self,
        origin_session_key: str,
        voice: str = "",
        goal: str = "",
        report_back_session_key: str | None = None,
    ) -> dict[str, str]:
        """Mint a new voice session token + URL for the given origin session.

        ``goal`` makes it a task-oriented call (the voice agent works toward it
        and reports back via report_success/report_failure). ``report_back_session_key``
        is an optional second dispatch target — when set, the summary lands in both
        the origin session and this one (used by reach_out_with_voice_call so the
        user who requested the reach-out gets the answer).
        """
        token = str(uuid4())
        now = utcnow().isoformat()
        await self.db.execute(
            """INSERT INTO voice_sessions
               (id, origin_session_key, voice, goal, report_back_session_key, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (token, origin_session_key, voice, goal, report_back_session_key, now),
        )
        base = self._get_settings().resolved_public_url
        url = f"{base}/voice/session.html?id={token}"
        logger.info(
            "Voice session created for %s (token=%s, goal=%s, report_back=%s)",
            origin_session_key, token[:8], bool(goal), bool(report_back_session_key),
        )
        return {"id": token, "url": url}

    async def find_active(self, origin_session_key: str) -> dict[str, Any] | None:
        """Return the current pending/active session for an origin, if any."""
        return await self.db.fetch_one(
            """SELECT * FROM voice_sessions
               WHERE origin_session_key = ? AND status IN ('pending', 'active')
               ORDER BY created_at DESC LIMIT 1""",
            (origin_session_key,),
        )

    async def cleanup_stale(self) -> int:
        """Mark any 'active' sessions as completed — their bridge processes are gone
        after a server restart, so reconnecting to them produces stale behavior."""
        now = utcnow().isoformat()
        count = await self.db.execute(
            "UPDATE voice_sessions SET status='completed', completed_at=? WHERE status='active'",
            (now,),
        )
        if count:
            logger.info("Cleaned up %d stale active voice sessions", count)
        return count

    async def resolve(self, token: str) -> dict[str, Any] | None:
        """Resolve a voice session token.

        A link stays valid until the call has completed and produced a result —
        no time expiry. Reconnection to an active (incomplete) session is allowed,
        so a dropped browser tab or a failed attempt can retry the same link.
        Only ``completed`` (or explicitly ``expired``) sessions are rejected.
        """
        row = await self.db.fetch_one(
            "SELECT * FROM voice_sessions WHERE id = ?", (token,),
        )
        if row is None:
            return None
        if row["status"] in ("completed", "expired"):
            return None
        # pending → active on first activation. active stays active (reconnection).
        if row["status"] == "pending":
            now = utcnow().isoformat()
            await self.db.execute(
                "UPDATE voice_sessions SET status='active', activated_at=? WHERE id=?",
                (now, token),
            )
        return row

    async def build_instructions(self, row: dict[str, Any]) -> str:
        """Build the Realtime session instructions: persona + voice preamble + recent chat context."""
        from bob_server.services.prompt_assembler import load_workspace_prompt
        from bob_server.services.session_service import SessionService

        settings = self._get_settings()
        persona = await load_workspace_prompt(settings.harness.workspace_dir, db=self.db)

        origin = row["origin_session_key"]
        msgs = await SessionService(self.ctx).get_messages(origin, limit=6)
        context_lines = []
        for m in msgs:
            if not (m.content or "").strip():
                continue
            speaker = "User" if m.role == "user" else "Bob"
            context_lines.append(f"{speaker}: {m.content.strip()}")
        context_block = "\n".join(context_lines[-6:]) if context_lines else "(no recent messages)"

        goal = (row.get("goal") or "").strip()
        goal_block = ""
        if goal:
            goal_block = (
                "\n\n--- Your goal on this call ---\n"
                f"{goal}\n\n"
                "You MUST have a real conversation to achieve this — ask questions, "
                "listen to the answers, and engage naturally over multiple turns. "
                "Do NOT call report_success or report_failure until you have actually "
                "spoken with the person and completed the task through conversation. "
                "When the task is genuinely done, call report_success with the outcome "
                "and key facts in details. If you cannot complete it, call report_failure."
            )

        return (
            f"{persona}{goal_block}\n\n--- Voice call mode ---\n{_VOICE_PREAMBLE}"
            f"\n\n--- Recent chat context ---\n{context_block}"
        )

    async def complete(
        self,
        token: str,
        transcript: str,
        duration_seconds: float,
        *,
        wa_service: "WhatsAppBridgeService | None" = None,
    ) -> None:
        """Persist transcript, summarise, and dispatch the summary back to the origin session."""
        from bob_server.services.session_service import SessionService
        from bob_server.services.llm_dispatch import LLMDispatchService
        from bob_server.services.thread_result_service import dispatch_thread_result

        row = await self.db.fetch_one("SELECT * FROM voice_sessions WHERE id = ?", (token,))
        if row is None:
            return
        origin = row["origin_session_key"]

        # Store the transcript as a session message (channel='voice_realtime') for memory.
        if transcript.strip():
            await SessionService(self.ctx).add_message(
                origin, "assistant", transcript,
                channel="voice_realtime",
                metadata={"voice_session": token, "duration_seconds": duration_seconds},
            )

        # Mark completed.
        now = utcnow().isoformat()
        await self.db.execute(
            """UPDATE voice_sessions
               SET status='completed', transcript=?, duration_seconds=?, completed_at=?
               WHERE id=?""",
            (transcript, duration_seconds, now, token),
        )

        # Summarise, then hand to the standard thread-result dispatch so Bob relays it.
        summary = await self._summarise(transcript, duration_seconds)
        result_content = (
            f"## Voice call with the user just ended\n"
            f"Duration: {duration_seconds:.0f}s\n\n"
            f"{summary}\n\n"
            f"Relay a short, friendly summary of this voice call to the user via "
            f"send_whatsapp_message. If anything was agreed or needs follow-up, mention it."
        )
        await dispatch_thread_result(
            self.ctx,
            origin_session_key=origin,
            result_content=result_content,
            call_category="voice_session",
            wa_service=wa_service,
        )

        # Goal-mode reach-out: also dispatch to the requesting user's session so
        # they get the answer. The origin session (contact's DM) keeps memory.
        report_back = row.get("report_back_session_key") if row else None
        if report_back and report_back != origin:
            await dispatch_thread_result(
                self.ctx,
                origin_session_key=report_back,
                result_content=result_content,
                call_category="voice_session_report",
                wa_service=wa_service,
            )

        logger.info("Voice session %s completed (%.0fs)", token[:8], duration_seconds)

    async def _summarise(self, transcript: str, duration_seconds: float) -> str:
        """Summarise the voice transcript in 2-4 sentences."""
        if not transcript.strip():
            return "The call ended before any conversation took place."
        from bob_server.services.llm_dispatch import LLMDispatchService
        messages = [
            {
                "role": "system",
                "content": (
                    "Summarise this voice call transcript between Bob and the user. "
                    "Focus on what was discussed and anything agreed or decided. "
                    "Be concise (2-4 sentences). If nothing meaningful happened, say so."
                ),
            },
            {
                "role": "user",
                "content": f"Duration: {duration_seconds:.0f}s\n\nTranscript:\n{transcript}",
            },
        ]
        try:
            result = await LLMDispatchService(self.ctx).chat(
                messages, call_category="voice_session_summary", session_key=None,
            )
            return result.strip()
        except Exception as e:
            logger.warning("Voice summary failed (%s); using truncated transcript", e)
            return transcript[:300]

    async def _set_status(self, token: str, status: str) -> None:
        await self.db.execute(
            "UPDATE voice_sessions SET status=? WHERE id=?", (status, token),
        )
