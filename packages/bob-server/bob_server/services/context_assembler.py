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
        membership; DMs and email threads use participants.

        ``include_identifier`` reproduces the email format (``Name <addr>``);
        WhatsApp omits identifiers (names only).
        """
        if ":group:" in session_key:
            rich = await self._group_participants_prompt(session_key)
            if rich:
                return rich

        from bob_server.repositories.participants import ParticipantRepository
        rows = await ParticipantRepository(self.db).list_for(session_key)
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
        from bob_server.repositories.conversations import ConversationRepository
        route = await ConversationRepository(self.db).route_for(session_key)
        if not (route and route["address"]):
            return ""
        from bob_server.repositories.groups import GroupRepository
        groups = GroupRepository(self.db)
        group = await groups.get_by_jid(route["address"])
        if not group:
            return ""
        members = await groups.members_with_contacts(group["id"])
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
        from bob_server.repositories.conversations import ConversationRepository
        eid = await ConversationRepository(self.db).group_memory_entity_id(session_key)
        if not eid:
            return ""
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
        """Active-outreach block from the conversation's outreach goal."""
        from bob_server.repositories.goals import GoalRepository
        goal = await GoalRepository(self.db).active_outreach(session_key)
        if not goal:
            return ""
        try:
            strategy = json.loads(goal["strategy_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            strategy = {}
        return (
            "## Active Outreach Request\n"
            "You proactively sent a message to this contact.\n"
            f"- Requested by: {strategy.get('requestor', 'unknown')}\n"
            f"- Objective: {goal['objective'] or 'unknown'}\n"
            f"- Your initial message: \"{strategy.get('message', '')}\"\n\n"
            "Your goal is to achieve the objective through this conversation. "
            "When you have the information needed, call the finish_outreach tool to relay the result back."
        )
