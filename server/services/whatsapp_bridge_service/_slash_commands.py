"""Slash command handlers extracted from WhatsAppBridgeService.

Mixin: these methods rely on ``self.send_message``, ``self._cmd_*`` siblings,
and other instance state from the host class.
"""

from __future__ import annotations

import json
import logging

from server.services.base import utcnow
from server.services.whatsapp_bridge_service._media import _format_created_at  # noqa: F401 — re-exported via __init__


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
        elif command == "/model":
            await self._cmd_model(args, session_key, chat_id)
        else:
            await self.send_message(
                chat_id,
                f"{command} isn't a command — try /model, /patience, /relevance, "
                f"/verbose, /silentmem, /autoplan, /who")

    async def _cmd_patience(self, args: str, session_key: str, chat_id: str) -> None:
        """Toggle patience for the current session."""
        arg = args.strip().lower()
        if arg not in ("on", "off"):
            await self.send_message(chat_id, "Usage: /patience on|off")
            return

        enabled = arg == "on"
        from server.repositories.conversations import ConversationRepository
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
        from server.repositories.conversations import ConversationRepository
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
        """Reply with the persona's provenance. The persona is file-based
        now (self/bob/*.md healed from the repo bundle at boot) — there is
        no runtime revision number; git history is the history."""
        await self.send_message(
            chat_id,
            "persona: workspace/self/bob (files, healed from the repo bundle "
            "at boot — history is git)")

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

        from server.repositories.conversations import ConversationRepository
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
        from server.services.dream import config as dream_config

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
        from server.services.dream import DreamStore
        c = await DreamStore.from_db(self.db).autoplan_counters(session_key)
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

    async def _cmd_model(self, args: str, session_key: str, chat_id: str) -> None:
        """Switch the model for this conversation (runtime, no restart).

        Usage: /model [alias|model-slug|default]
        No args → status. 'default' (or reset/off) clears the override.
        Aliases come from {config_dir}/models.yaml, hot-reloaded — see
        services/model_registry.py. Applies to this chat's main dispatch
        turns only; memory/patience passes stay on their configured models.
        """
        from server.repositories.conversations import ConversationRepository
        from server.services import model_registry

        arg = args.strip()
        settings = self.ctx.settings
        repo = ConversationRepository(self.db)
        await repo.ensure(session_key)
        alias_map = model_registry.aliases(settings.config_dir)
        alias_list = ", ".join(f"{k} → {v}" for k, v in sorted(alias_map.items())) or "none configured"

        if arg in ("", "status"):
            policy = await repo.get_policy(session_key)
            current = (policy.get("model_override") or "").strip()
            if not current:
                current_line = f"model: {settings.openai.default_model} (default)"
            else:
                resolved = model_registry.resolve(current, settings.config_dir)
                shown = f"{current} → {resolved}" if current.lower() != resolved.lower() else current
                current_line = f"model: {shown} (/model default to revert)"
            await self.send_message(chat_id, f"{current_line}\naliases: {alias_list}")
            return

        if arg.lower() in ("default", "reset", "off"):
            await repo.set_policy(session_key, {"model_override": ""})
            logger.info("model override cleared for conversation %s", session_key)
            await self.send_message(chat_id, f"model reverted to default ({settings.openai.default_model})")
            return

        ok, result = model_registry.validate(
            arg, settings.config_dir, openrouter_enabled=settings.openrouter.enabled)
        if not ok:
            await self.send_message(chat_id, f"{result}\navailable aliases: {alias_list}")
            return

        await repo.set_policy(session_key, {"model_override": arg})
        logger.info("model override %s → %s for conversation %s", arg, result, session_key)
        await self.send_message(chat_id, f"model set to {arg} → {result} for this chat")

    async def _cmd_silentmem(self, session_key: str, chat_id: str) -> None:
        """Trigger a silent memory extraction turn on the current session now.

        Runs immediately with force=True (bypasses the undigested-message
        guard). Reply summarises what was recorded. If /verbose is on for the
        session, the extraction turn itself surfaces the per-claim breakdown.
        """
        from server.services.memory import MemoryService

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

