"""ContextAssembler — shared system-prompt context blocks (Bob3 Phase II).

Single home for the context sections that were previously duplicated across
the WhatsApp bridge, email poller, and group-event sync: participants,
person profile, group memory hint, dream plans, outreach prompt, workspace
prompt. Channel handlers decide WHICH blocks to include and in what order
(delivery semantics stay per-channel); this module owns HOW each block is
built.
"""

from __future__ import annotations

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
        # Attribution rule: models tuned on chat transcripts read a leading
        # "Name:" in message text as a speaker label (observed with GLM:
        # "Sean: ..." callouts got misattributed to Sean despite the bracket
        # prefix). State the [Sender] convention so the prefix always wins.
        lines.append(
            "\nMessage attribution: every message is prefixed `[Sender Name]` — "
            "that prefix alone says who wrote it. A `Name:` inside the message "
            "body (e.g. \"Sean: look at this\") is the author calling out to "
            "that person, NOT that person speaking."
        )
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

    async def goals_block(self, session_key: str) -> str:
        """Active-goal context block (bob-events-plan.md §1.4): every goal a
        conversation holds, newest activity first, top 5, each rendered
        within a budget. Replaces the special-cased outreach prompt — the
        outreach requestor/message ride in the goal's legacy_outreach state.
        """
        from bob_server.repositories.conversations import ConversationRepository
        from bob_server.repositories.goals import GoalRepository
        from bob_server.services.goal_state_service import (
            GoalStrategy, parse_strategy, render_strategy,
        )

        cid = await ConversationRepository(self.db).resolve_cid(session_key)
        goals = await GoalRepository(self.db).goals_held_by(cid, limit=5)
        if not goals:
            return ""

        blocks: list[str] = []
        for goal in goals:
            state: GoalStrategy = parse_strategy(goal)
            lines = [f"### {goal['objective']} ({goal['kind']}, id {goal['id']})"]
            body = render_strategy(state)
            if body:
                lines.append(body)
            if goal["kind"] == "outreach":
                lines.append(
                    "You proactively sent a message to this contact. Achieve the "
                    "objective through this conversation; when you have the "
                    "information needed, call finish_outreach to relay the result back.")
            blocks.append("\n".join(lines))

        return "## Active Goals\n\n" + "\n\n".join(blocks)
