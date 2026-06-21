"""Slash command handlers extracted from WhatsAppBridgeService.

Mixin: these methods rely on ``self.send_message``, ``self._cmd_*`` siblings,
and other instance state from the host class.
"""

from __future__ import annotations

import json
import logging
from uuid import uuid4

from bob_server.services.base import utcnow
from bob_server.services.whatsapp_bridge_service._media import _format_created_at, _jid_to_phone


logger = logging.getLogger(__name__)


class SlashCommandsMixin:
    """Slash command handlers (`/patience`, `/who`, `/bulletin`)."""

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
        elif command == "/bulletin":
            await self._cmd_bulletin(args, session_key, chat_id, chat_kind)
        elif command == "/who":
            await self._cmd_who(chat_id)
        elif command == "/approve":
            await self._cmd_approve(args, chat_id)
        elif command == "/verbose":
            await self._cmd_verbose(args, session_key, chat_id)
        elif command == "/silentmem":
            await self._cmd_silentmem(session_key, chat_id)

    async def _cmd_patience(self, args: str, session_key: str, chat_id: str) -> None:
        """Toggle patience for the current session."""
        arg = args.strip().lower()
        if arg not in ("on", "off"):
            await self.send_message(chat_id, "Usage: /patience on|off")
            return

        enabled = arg == "on"
        route = await self.db.fetch_one(
            "SELECT id, metadata FROM session_routes WHERE session_key = ? AND deleted_at IS NULL AND is_active = 1",
            (session_key,),
        )
        if not route:
            await self.send_message(chat_id, "No session route found")
            return

        meta = json.loads(route["metadata"]) if route["metadata"] else {}
        meta["patience_enabled"] = enabled
        await self.db.execute(
            "UPDATE session_routes SET metadata = ?, updated_at = ? WHERE id = ?",
            (json.dumps(meta), utcnow().isoformat(), route["id"]),
        )
        logger.info("patience %s for session %s (route %s)", arg, session_key, route["id"])

        status = "enabled — waiting for silence before responding" if enabled else "disabled — responding immediately"
        await self.send_message(chat_id, f"Patience {status}")

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

    async def _cmd_approve(self, args: str, chat_id: str) -> None:
        """Pre-create a contact so an unknown number can DM Bob.

        Usage: /approve <phone> [name...]
        Trust flag stays at 0 — the gate is contact existence, not trust.
        """
        parts = args.strip().split(None, 1)
        if not parts or not parts[0]:
            await self.send_message(chat_id, "Usage: /approve <phone> [name]")
            return
        raw_phone = parts[0]
        name = parts[1].strip() if len(parts) > 1 else ""
        # _jid_to_phone handles +CC, leading-0, and bare international forms.
        phone_number = _jid_to_phone(raw_phone)
        if not phone_number:
            await self.send_message(chat_id, f"Could not parse phone: {raw_phone}")
            return

        existing = await self.db.fetch_one(
            "SELECT id FROM contacts WHERE phone_number = ? AND deleted_at IS NULL LIMIT 1",
            (phone_number,),
        )
        if existing:
            if name:
                await self.db.execute(
                    "UPDATE contacts SET name = ?, updated_at = ? WHERE id = ?",
                    (name, utcnow().isoformat(), existing["id"]),
                )
                from bob_server.services.memory import MemoryService
                await MemoryService(self.ctx).sync_person_display_name_for_contact(
                    existing["id"], name,
                )
            await self.send_message(chat_id, f"/approve: already a contact ({phone_number})")
            return

        new_id = str(uuid4())
        now_iso = utcnow().isoformat()
        await self.db.execute(
            """INSERT INTO contacts (id, name, phone_number, is_trusted, created_at, updated_at)
               VALUES (?, ?, ?, 0, ?, ?)""",
            (new_id, name or phone_number, phone_number, now_iso, now_iso),
        )

        from bob_server.services.memory import MemoryService
        mem_svc = MemoryService(self.ctx)
        await mem_svc.ensure_person_entry(
            self.ctx.settings.harness.workspace_dir,
            contact_id=new_id, name=name or phone_number,
            phone_number=phone_number, channel="WhatsApp",
        )

        logger.info("/approve: created contact %s for phone %s", new_id, phone_number)
        await self.send_message(chat_id, f"/approve: added {name or phone_number} ({phone_number})")

    async def _cmd_bulletin(self, args: str, session_key: str, chat_id: str, chat_kind: str) -> None:
        """Generate bulletins for the current session on demand."""
        from bob_server.services.memory import MemoryService

        settings = self._get_settings()
        workspace = settings.harness.workspace_dir
        svc = MemoryService(self.ctx)

        try:
            result = await svc.generate_session_bulletins(
                workspace, session_key, run_dream=True,
            )
        except Exception as exc:
            logger.exception("/bulletin failed for %s", session_key)
            await self.send_message(chat_id, f"/bulletin error: {exc}")
            return

        status = result.get("status", "unknown")
        if status == "empty":
            reason = result.get("reason", "no data")
            await self.send_message(chat_id, f"/bulletin: nothing to process ({reason})")
            return

        n = result.get("bulletins_generated", 0)
        msgs = result.get("messages_processed", 0)
        dream = result.get("dream", {})
        claims = dream.get("claims_extracted", 0) if isinstance(dream, dict) else 0
        entities = dream.get("entity_ops", 0) if isinstance(dream, dict) else 0

        await self.send_message(
            chat_id,
            f"/bulletin: {n} bulletin(s) from {msgs} messages | "
            f"dream: {claims} claims, {entities} entity ops",
        )

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

        route = await self.db.fetch_one(
            "SELECT id, metadata FROM session_routes "
            "WHERE session_key = ? AND deleted_at IS NULL AND is_active = 1",
            (session_key,),
        )
        if not route:
            await self.send_message(chat_id, "No session route found")
            return

        meta = json.loads(route["metadata"]) if route["metadata"] else {}
        current = bool(meta.get("memory_verbose", False))

        if arg == "status" or arg == "":
            state = "ON" if current else "OFF"
            await self.send_message(chat_id, f"verbose {state}")
            return

        enabled = arg == "on"
        if enabled == current:
            state = "ON" if current else "OFF"
            await self.send_message(chat_id, f"verbose already {state}")
            return

        meta["memory_verbose"] = enabled
        await self.db.execute(
            "UPDATE session_routes SET metadata = ?, updated_at = ? WHERE id = ?",
            (json.dumps(meta), utcnow().isoformat(), route["id"]),
        )
        logger.info("verbose %s for session %s (route %s)", arg, session_key, route["id"])
        await self.send_message(chat_id, f"verbose {'ON' if enabled else 'OFF'}")

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

