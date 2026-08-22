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
    "The person joined by tapping a link, so let THEM speak first — they will "
    "usually say hello. Wait for their greeting before you introduce yourself. "
    "If the first thing you hear is silence, a brief noise, or something "
    "unintelligible, it was not a greeting — wait for a clear human voice.\n\n"
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
        subagent_id: str | None = None,
        phone_number: str = "",
    ) -> dict[str, str]:
        """Mint a new voice session token + URL for the given origin session.

        ``goal`` makes it a task-oriented call (the voice agent works toward it
        and reports back via report_success/report_failure). ``report_back_session_key``
        is an optional second dispatch target — when set, the summary lands in both
        the origin session and this one. ``subagent_id`` links the
        session to an openai_voice subagent so its completion marks the subagent done.
        ``phone_number`` is the contact's number, when known, for the calls UI.

        A mirror row is written to ``phone_calls`` (same id) so browser voice-link
        calls appear in the phone calls UI alongside Twilio calls — they run the
        same Realtime bridge and lifecycle. voice_sessions stays the source of
        truth; the phone_calls row is a UI-facing projection kept in sync at
        each lifecycle transition below.
        """
        token = str(uuid4())
        now = utcnow().isoformat()
        await self.db.execute(
            """INSERT INTO voice_sessions
               (id, origin_session_key, voice, goal, report_back_session_key, subagent_id, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (token, origin_session_key, voice, goal, report_back_session_key, subagent_id, now),
        )
        await self.db.execute(
            """INSERT INTO phone_calls
               (id, call_sid, phone_number, direction, status, agenda, engine,
                subagent_id, origin_session_key, started_at)
               VALUES (?, '', ?, 'voice_link', 'ringing', ?, 'openai_realtime', ?, ?, datetime('now'))""",
            (token, phone_number, goal, subagent_id, origin_session_key),
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
        """Sweep stale sessions at startup.

        - 'active' → completed: their bridge processes are gone after a server
          restart, so reconnecting produces stale behavior.
        - 'pending' older than LINK_TTL_HOURS → expired: the link was never
          tapped (a phone call usually went out instead); without this they
          show as 'ringing' in the calls UI forever.
        """
        now = utcnow().isoformat()
        count = await self.db.execute(
            "UPDATE voice_sessions SET status='completed', completed_at=? WHERE status='active'",
            (now,),
        )
        if count:
            await self.db.execute(
                """UPDATE phone_calls
                   SET status='completed', completed_at=datetime('now')
                   WHERE direction='voice_link' AND status='active'""",
            )
        expired = await self.db.execute(
            "UPDATE voice_sessions SET status='expired', completed_at=? "
            "WHERE status='pending' AND created_at < datetime('now', ?)",
            (now, f"-{self.LINK_TTL_HOURS} hours"),
        )
        if expired:
            await self.db.execute(
                "UPDATE phone_calls SET status='canceled', completed_at=datetime('now') "
                "WHERE direction='voice_link' AND status='ringing' AND started_at < datetime('now', ?)",
                (f"-{self.LINK_TTL_HOURS} hours",),
            )
        total = count + expired
        if total:
            logger.info("Cleaned up %d stale voice sessions (%d expired links)", total, expired)
        return total

    LINK_TTL_HOURS = 24

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
            await self.db.execute(
                "UPDATE phone_calls SET status='active' WHERE id = ? AND direction='voice_link'",
                (token,),
            )
        return row

    async def persist_transcript(self, token: str, transcript: str) -> None:
        """Persist a partial transcript after a turn boundary (best-effort).

        Keeps the dashboard / DB readable mid-call and preserves progress if
        the bridge hangs on cleanup.
        """
        try:
            await self.db.execute(
                "UPDATE voice_sessions SET transcript = ? WHERE id = ?",
                (transcript, token),
            )
            await self.db.execute(
                "UPDATE phone_calls SET transcript = ? WHERE id = ? AND direction='voice_link'",
                (transcript, token),
            )
        except Exception:
            logger.warning("Failed to persist partial voice transcript", exc_info=True)

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
        tool_calls: list[dict[str, Any]] | None = None,
        wa_service: "WhatsAppBridgeService | None" = None,
    ) -> None:
        """Persist transcript, summarise, and dispatch the summary back to the origin session.

        ``tool_calls`` is the bridge's recorded tool-call list; a
        report_success / report_failure result is extracted into the structured
        ``outcome`` column (shared logic with the phone path).
        """
        import json as _json

        from bob_server.services.session_service import SessionService
        from bob_server.services.wake_service import wake_conversation
        from bob_server.services.voice_dispatch_service import (
            extract_outcome,
            mark_voice_subagent_complete,
        )

        row = await self.db.fetch_one("SELECT * FROM voice_sessions WHERE id = ?", (token,))
        if row is None:
            return
        origin = row["origin_session_key"]

        outcome = extract_outcome(tool_calls)

        # Store the transcript as a session message (channel='voice_realtime') for memory.
        if transcript.strip():
            await SessionService(self.ctx).add_message(
                origin, "assistant", transcript,
                channel="voice_realtime",
                metadata={"voice_session": token, "duration_seconds": duration_seconds},
            )

        # Mark completed (voice_sessions is the source of truth; the phone_calls
        # mirror row carries the same data for the calls UI).
        now = utcnow().isoformat()
        await self.db.execute(
            """UPDATE voice_sessions
               SET status='completed', transcript=?, duration_seconds=?, outcome=?, completed_at=?
               WHERE id=?""",
            (transcript, duration_seconds,
             _json.dumps(outcome) if outcome else None, now, token),
        )
        await self.db.execute(
            """UPDATE phone_calls
               SET status='completed', transcript=?, duration_seconds=?, outcome=?, completed_at=datetime('now')
               WHERE id=? AND direction='voice_link'""",
            (transcript, duration_seconds,
             _json.dumps(outcome) if outcome else None, token),
        )

        # Summarise, then hand to the standard thread-result dispatch so Bob relays it.
        summary = await self._summarise(transcript, duration_seconds, outcome)
        result_content = (
            f"## Voice call with the user just ended\n"
            f"Duration: {duration_seconds:.0f}s\n\n"
            f"{summary}\n\n"
            f"Relay a short, friendly summary of this voice call to the user via "
            f"send_whatsapp_message. If anything was agreed or needs follow-up, mention it."
        )
        await wake_conversation(
            self.ctx, origin, result_content,
            call_category="voice_session",
        )

        # Goal-mode reach-out: also dispatch to the requesting user's session so
        # they get the answer. The origin session (contact's DM) keeps memory.
        report_back = row.get("report_back_session_key") if row else None
        if report_back and report_back != origin:
            await wake_conversation(
                self.ctx, report_back, result_content,
                call_category="voice_session_report",
            )

        # If this session was dispatched by an openai_voice subagent, mark it
        # completed so the parent LLM sees a clean lifecycle. Shared with the
        # phone path (voice_dispatch_service).
        subagent_id = row.get("subagent_id") if row else None
        if subagent_id:
            await mark_voice_subagent_complete(self.db, subagent_id, transcript)

        logger.info("Voice session %s completed (%.0fs)", token[:8], duration_seconds)

    async def _summarise(
        self,
        transcript: str,
        duration_seconds: float,
        outcome: dict[str, Any] | None = None,
    ) -> str:
        """Summarise the voice transcript in 2-4 sentences."""
        if not transcript.strip() and not outcome:
            return "The call ended before any conversation took place."
        from bob_server.services.llm_dispatch import LLMDispatchService
        from bob_server.services.voice_dispatch_service import format_outcome

        outcome_block = format_outcome(outcome)
        transcript_block = (
            f"Reported outcome:\n{outcome_block}\n\nTranscript:\n{transcript}"
            if outcome_block else transcript
        )
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
                "content": f"Duration: {duration_seconds:.0f}s\n\n{transcript_block}",
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
