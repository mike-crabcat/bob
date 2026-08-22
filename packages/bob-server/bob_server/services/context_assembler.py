"""ContextAssembler — shared system-prompt context blocks (Bob3 Phase II).

Single home for the context sections that were previously duplicated across
the WhatsApp bridge, email poller, and group-event sync: participants,
person profile, group memory hint, dream plans, outreach prompt, workspace
prompt. Channel handlers decide WHICH blocks to include and in what order
(delivery semantics stay per-channel); this module owns HOW each block is
built.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class ContextAssembler:
    def __init__(self, ctx: Any):
        self.ctx = ctx
        self.db = ctx.db

    @staticmethod
    def compose(*parts: str) -> str:
        return "\n\n".join(p for p in parts if p)

    async def workspace_prompt(self) -> str:
        from bob_server.services.prompt_assembler import load_workspace_prompt
        return await load_workspace_prompt(
            self.ctx.settings.harness.workspace_dir, db=self.db)

    async def participants_prompt(
            self, session_key: str, *, include_identifier: bool = False) -> str:
        """Participants block. Group sessions prefer the rich whatsappgroups
        membership; DMs and email threads use session_participants.

        ``include_identifier`` reproduces the email format (``Name <addr>``);
        WhatsApp omits identifiers (names only).
        """
        if ":group:" in session_key:
            rich = await self._group_participants_prompt(session_key)
            if rich:
                return rich

        rows = await self.db.fetch_all(
            "SELECT display_name, identifier, contact_id, is_trusted, last_active_at "
            "FROM session_participants WHERE session_key = ? ORDER BY last_active_at DESC",
            (session_key,))
        if not rows:
            return ""
        lines = ["## Participants"]
        for r in rows:
            name = r["display_name"] or r["identifier"]
            if include_identifier:
                if r["contact_id"]:
                    trust = "trusted" if r["is_trusted"] else "untrusted"
                    lines.append(f"- {name} <{r['identifier']}> (contact, {trust})")
                else:
                    lines.append(f"- {name} <{r['identifier']}> (not in contacts)")
            else:
                if r["contact_id"]:
                    trust = "trusted" if r["is_trusted"] else "untrusted"
                    lines.append(f"- {name} (contact, {trust})")
                else:
                    lines.append(f"- {name} ({r['identifier']}, not in contacts)")
        return "\n".join(lines)

    async def _group_participants_prompt(self, session_key: str) -> str:
        route = await self.db.fetch_one(
            "SELECT chat_id FROM session_routes WHERE session_key = ?",
            (session_key,))
        if not (route and route["chat_id"]):
            return ""
        group = await self.db.fetch_one(
            "SELECT id, name, member_count FROM whatsappgroups "
            "WHERE whatsapp_jid = ? AND deleted_at IS NULL",
            (route["chat_id"],))
        if not group:
            return ""
        members = await self.db.fetch_all(
            """SELECT gm.display_name, gm.is_admin, gm.is_super_admin,
                      c.name as contact_name, c.is_trusted
               FROM whatsappgroup_members gm
               JOIN contacts c ON c.id = gm.contact_id AND c.deleted_at IS NULL
               WHERE gm.group_id = ? AND gm.left_at IS NULL
               ORDER BY gm.is_super_admin DESC, gm.is_admin DESC, gm.display_name ASC""",
            (group["id"],))
        if not members:
            return ""
        lines = [f"## Participants ({len(members)} members in {group['name'] or 'group'})"]
        for m in members:
            name = m["display_name"] or m["contact_name"] or "Unknown"
            badges = []
            if m["is_super_admin"]:
                badges.append("super admin")
            elif m["is_admin"]:
                badges.append("admin")
            badges.append("trusted" if m["is_trusted"] else "untrusted")
            lines.append(f"- {name} ({', '.join(badges)})")
        return "\n".join(lines)

    async def person_profile(self, contact_id: str | None) -> str:
        """Person-memory profile block for DM sessions."""
        if not contact_id:
            return ""
        from bob_server.services.memory import MemoryService
        entry = await MemoryService(self.ctx).find_person_entry(
            self.ctx.settings.harness.workspace_dir, contact_id=contact_id)
        return f"## Person Profile\n\n{entry}" if entry else ""

    async def group_memory_hint(self, session_key: str) -> str:
        """Recall hint for groups with an accumulated memory entity."""
        group_row = await self.db.fetch_one(
            "SELECT wg.memory_entity_id FROM whatsappgroups wg "
            "JOIN session_routes sr ON sr.chat_id = wg.whatsapp_jid "
            "WHERE sr.session_key = ? AND wg.deleted_at IS NULL",
            (session_key,))
        if not (group_row and group_row["memory_entity_id"]):
            return ""
        eid = group_row["memory_entity_id"]
        return (
            "## Group Memory\n\n"
            f"This is a WhatsApp group with accumulated memory entity `{eid}`.\n"
            f"Use `recall('{eid}')` to look up group knowledge."
        )

    async def dream_plans_prompt(self, session_key: str) -> str:
        from bob_server.services.dream.injection import build_session_plans_prompt
        return await build_session_plans_prompt(
            self.db, session_key, dream_enabled=self.ctx.settings.dream.enabled)

    async def outreach_prompt(self, session_key: str) -> str:
        """Active-outreach block from session_routes metadata (WhatsApp)."""
        route = await self.db.fetch_one(
            "SELECT metadata FROM session_routes WHERE session_key = ?",
            (session_key,))
        if not (route and route["metadata"]):
            return ""
        try:
            route_meta = json.loads(route["metadata"])
        except (json.JSONDecodeError, TypeError):
            route_meta = {}
        if "outreach_initiated_from" not in route_meta:
            return ""
        return (
            "## Active Outreach Request\n"
            "You proactively sent a message to this contact.\n"
            f"- Requested by: {route_meta.get('outreach_requestor', 'unknown')}\n"
            f"- Objective: {route_meta.get('outreach_objective', 'unknown')}\n"
            f"- Your initial message: \"{route_meta.get('outreach_message', '')}\"\n\n"
            "Your goal is to achieve the objective through this conversation. "
            "When you have the information needed, call the finish_outreach tool to relay the result back."
        )
