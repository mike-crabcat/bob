"""Slash command handlers extracted from WhatsAppBridgeService.

Mixin: these methods rely on ``self.send_message``, ``self._cmd_*`` siblings,
and other instance state from the host class.
"""

from __future__ import annotations

import json
import logging

from bob_server.services.base import utcnow
from bob_server.services.whatsapp_bridge_service._media import _format_created_at


logger = logging.getLogger(__name__)


class SlashCommandsMixin:
    """Slash command handlers (`/patience`, `/who`, etc.)."""

    async def _handle_slash_command(
        self, text: str, session_key: str, chat_id: str,
        chat_kind: str, sender_jid: str, sender_name: str,
    ) -> None:
        """Handle slash commands from trusted contacts."""
        parts = text.strip().split(None, 1)
        command = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""
        logger.info("slash command from %s in %s: %s %s", sender_name, session_key, command, args)

        if command == "/patience":
            await self._cmd_patience(args, session_key, chat_id)
        elif command == "/relevance":
            await self._cmd_relevance(args, session_key, chat_id)
        elif command == "/who":
            await self._cmd_who(chat_id)
        elif command == "/verbose":
            await self._cmd_verbose(args, session_key, chat_id)
        elif command == "/silentmem":
            await self._cmd_silentmem(session_key, chat_id)
        elif command == "/autoplan":
            await self._cmd_autoplan(args, session_key, chat_id)

    async def _cmd_patience(self, args: str, session_key: str, chat_id: str) -> None:
        """Toggle patience for the current session."""
        arg = args.strip().lower()
        if arg not in ("on", "off"):
            await self.send_message(chat_id, "Usage: /patience on|off")
            return

        enabled = arg == "on"
        from bob_server.repositories.conversations import ConversationRepository
        repo = ConversationRepository(self.db)
        await repo.ensure(session_key)
        await repo.set_policy(session_key, {"patience_enabled": enabled})
        logger.info("patience %s for conversation %s", arg, session_key)

        status = "enabled — waiting for silence before responding" if enabled else "disabled — responding immediately"
        await self.send_message(chat_id, f"Patience {status}")

    async def _cmd_relevance(self, args: str, session_key: str, chat_id: str) -> None:
        """Toggle the patience relevance gate for the current session.

        When enabled (and `/patience on`), the patience LLM also decides whether
        to respond at all. Messages judged not addressed to Bob are marked
        dispatched without invoking the main LLM. Requires patience to be on.
        """
        arg = args.strip().lower()
        if arg not in ("on", "off"):
            await self.send_message(chat_id, "Usage: /relevance on|off")
            return

        enabled = arg == "on"
        from bob_server.repositories.conversations import ConversationRepository
        repo = ConversationRepository(self.db)
        await repo.ensure(session_key)
        policy = await repo.get_policy(session_key)
        patience_on = bool(policy.get("patience_enabled", False))
        await repo.set_policy(session_key, {"patience_relevance_gating": enabled})
        logger.info("relevance %s for conversation %s", arg, session_key)

        note = "" if patience_on else " (note: /patience is currently OFF — enable that first)"
        status = "enabled — patience LLM may skip dispatch entirely" if enabled else "disabled — patience LLM only decides timing"
        await self.send_message(chat_id, f"Relevance gate {status}{note}")

    async def _cmd_who(self, chat_id: str) -> None:
        """Reply with the active persona revision and creation timestamp."""
        row = await self.db.fetch_one(
            "SELECT revision, created_at FROM persona_records WHERE is_active = 1"
        )
        if row is None:
            await self.send_message(chat_id, "no active persona — using built-in defaults")
            return
        created = _format_created_at(row["created_at"])
        await self.send_message(chat_id, f"r{row['revision']} (created {created})")

    async def _cmd_verbose(self, args: str, session_key: str, chat_id: str) -> None:
        """Toggle verbose memory-extraction notices for this session.

        Usage: /verbose on|off|status
        When on, every silent extraction turn that creates entities or claims
        posts a [memory] system notice back to this chat listing them.
        """
        arg = args.strip().lower()
        if arg not in ("on", "off", "status", ""):
            await self.send_message(chat_id, "Usage: /verbose on|off|status")
            return

        from bob_server.repositories.conversations import ConversationRepository
        repo = ConversationRepository(self.db)
        await repo.ensure(session_key)
        policy = await repo.get_policy(session_key)
        current = bool(policy.get("memory_verbose", False))

        if arg == "status" or arg == "":
            state = "ON" if current else "OFF"
            await self.send_message(chat_id, f"verbose {state}")
            return

        enabled = arg == "on"
        if enabled == current:
            state = "ON" if current else "OFF"
            await self.send_message(chat_id, f"verbose already {state}")
            return

        await repo.set_policy(session_key, {"memory_verbose": enabled})
        logger.info("verbose %s for conversation %s", arg, session_key)
        await self.send_message(chat_id, f"verbose {'ON' if enabled else 'OFF'}")

    async def _cmd_autoplan(self, args: str, session_key: str, chat_id: str) -> None:
        """Toggle auto-approval of dream plans FOR THIS CHAT (runtime, no restart).

        Usage: /autoplan on|off|status
        Session-scoped: plans whose evidence came from this conversation
        auto-approve and get announced here. Other chats are unaffected.
        Outreach (calls/emails to new people) stays off regardless — that needs
        a separate per-plan flip.
        """
        from bob_server.services.dream import config as dream_config

        arg = args.strip().lower()
        if arg not in ("on", "off", "status", ""):
            await self.send_message(chat_id, "Usage: /autoplan on|off|status")
            return

        if arg in ("on", "off"):
            ok = await dream_config.set_session_autoplan(self.db, session_key, arg == "on")
            if not ok:
                await self.send_message(chat_id, "No session route found for this chat")
                return
            logger.info("autoplan %s for session %s (route metadata)", arg, session_key)

        current = await dream_config.get_session_autoplan(
            self.db, session_key, self.ctx.settings.dream.auto_approve_plans
        )
        counters = await self.db.fetch_one(
            """SELECT
                 SUM(CASE WHEN p.status = 'draft' THEN 1 ELSE 0 END) AS drafts,
                 SUM(CASE WHEN p.status = 'approved' AND p.announced_at IS NULL THEN 1 ELSE 0 END) AS pending,
                 SUM(CASE WHEN p.announced_at IS NOT NULL THEN 1 ELSE 0 END) AS announced
               FROM dream_plans p
               JOIN dream_item_links l ON l.item_type = 'plan' AND l.item_id = p.id
               WHERE l.session_key = ?""",
            (session_key,),
        )
        c = counters or {}
        counts = f"({c.get('drafts') or 0} draft, {c.get('pending') or 0} awaiting announce, {c.get('announced') or 0} announced)"
        if arg == "on":
            await self.send_message(
                chat_id,
                f"autoplan ON for this chat — plans from this conversation auto-approve and get "
                f"announced here; other chats unaffected, outreach stays off. {counts}",
            )
        elif arg == "off":
            await self.send_message(chat_id, f"autoplan OFF for this chat — plans await manual approval. {counts}")
        else:
            state = "ON" if current else "OFF"
            await self.send_message(chat_id, f"autoplan {state} for this chat")

    async def _cmd_silentmem(self, session_key: str, chat_id: str) -> None:
        """Trigger a silent memory extraction turn on the current session now.

        Runs immediately with force=True (bypasses the undigested-message
        guard). Reply summarises what was recorded. If /verbose is on for the
        session, the extraction turn itself surfaces the per-claim breakdown.
        """
        from bob_server.services.memory import MemoryService

        svc = MemoryService(self.ctx)
        try:
            result = await svc.run_silent_turn_extraction(
                session_key, force=True, trigger="silentmem",
            )
        except Exception as exc:
            logger.exception("/silentmem failed for %s", session_key)
            await self.send_message(chat_id, f"/silentmem error: {exc}")
            return

        status = result.get("status", "unknown")
        if status != "ok":
            reason = result.get("reason", "unknown")
            await self.send_message(chat_id, f"/silentmem: {status} ({reason})")
            return

        claims = result.get("claims_created", 0)
        entities = result.get("entities_created", 0)
        if not claims and not entities:
            await self.send_message(chat_id, "/silentmem: nothing recorded")
            return
        await self.send_message(
            chat_id,
            f"/silentmem: {claims} claim(s), {entities} entit(y/ies) recorded",
        )

