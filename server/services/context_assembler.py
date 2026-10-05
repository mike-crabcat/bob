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
        from server.services.prompt_assembler import load_workspace_prompt
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

        from server.repositories.participants import ParticipantRepository
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
        from server.repositories.conversations import ConversationRepository
        route = await ConversationRepository(self.db).route_for(session_key)
        if not (route and route["address"]):
            return ""
        from server.repositories.groups import GroupRepository
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
        # Quote-replies: the [reply to …] marker is WhatsApp quote context —
        # what the sender is replying to, not something they said.
        lines.append(
            "A `[reply to Name: \"…\"]` marker quotes the earlier message the "
            "sender is replying to — treat that snippet as context, not as "
            "part of their new message. `Bob (you)` means one of your own "
            "earlier messages."
        )
        return "\n".join(lines)

    async def person_profile(self, contact_id: str | None) -> str:
        """Person-memory profile block for DM sessions."""
        if not contact_id:
            return ""
        from server.services.memory import MemoryService
        entry = await MemoryService(self.ctx).find_person_entry(
            self.ctx.settings.harness.workspace_dir, contact_id=contact_id)
        return f"## Person Profile\n\n{entry}" if entry else ""

    async def group_memory_hint(self, session_key: str) -> str:
        """Recall hint + pushed expectations for groups with an accumulated
        memory entity. The norms/traditions/open-tasks push (2026-09-13)
        rides along ungated: recall sat at 1.7% of group turns, so the
        context a planning kickoff needs is rendered, not pulled."""
        from server.repositories.conversations import ConversationRepository
        eid = await ConversationRepository(self.db).group_memory_entity_id(session_key)
        if not eid:
            return ""
        parts = [
            "## Group Memory\n",
            f"This is a WhatsApp group with accumulated memory entity `{eid}`.\n"
            f"Use `recall('{eid}')` to look up group knowledge.",
        ]
        from server.services.memory.service import build_group_expectations
        pushed = await build_group_expectations(self.db, session_key)
        if pushed:
            parts.append(pushed)
        return "\n\n".join(parts)

    async def maybe_memory_roster(self, session_key: str) -> str:
        """Gated entry point for the conversation memory roster.

        Off unless the conversation opted in via the `memory_roster` policy
        flag; the BOB_MEMORY_ROSTER=off kill switch force-disables globally.
        Groups only — DMs already get person_profile. The roster itself is
        built by memory.build_conversation_roster (SQL ownership: the
        memory tables belong to services/memory/).
        """
        if not self.ctx.settings.memory.roster_enabled:
            return ""
        if ":group:" not in session_key:
            return ""
        from server.repositories.conversations import ConversationRepository
        policy = await ConversationRepository(self.db).get_policy(session_key)
        if not policy.get("memory_roster"):
            return ""
        from server.services.memory.service import build_conversation_roster
        return await build_conversation_roster(self.db, session_key)

    async def goals_block(self, session_key: str) -> str:
        """The conversation's work context (commitments plan Phase 3): ONE
        "Work" block — goals held, promises owed TO this conversation,
        suggestions offered here, promises asked OF it, and work running in
        the background."""
        from server.repositories.conversations import ConversationRepository
        from server.repositories.goals import GoalRepository

        cid = await ConversationRepository(self.db).resolve_cid(session_key)
        # kind='subagent' goals are executor bookkeeping, not intent — their
        # live state renders in the background section instead (Phase 0).
        goals = await GoalRepository(self.db).goals_held_by(
            cid, limit=5, exclude_kinds=("subagent",))
        background = await self._background_block(session_key)
        return await self._work_block(session_key, goals, background)

    @staticmethod
    def _goals_section(goals: list[dict]) -> str:
        from server.services.goal_state_service import (
            GoalStrategy, parse_strategy, render_strategy,
        )
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
        return (
            "\n\n".join(blocks) +
            "\n\nIf this conversation decides, agrees, or learns anything about "
            "one of these goals — including on behalf of the owner or a third "
            "party (e.g. someone relaying another person's confirmation) — you "
            "MUST write it to the goal (update_goal / update_goal_state): the "
            "goal's room only knows what is written to the goal, and a "
            "decision recorded only as memory never reaches it (the 2026-09-25 "
            "figurine doc: two agreements known to this conversation were "
            "absent from the goal's final artefact).\n\n"
            "WORK that belongs to one of these goals (renders, models, "
            "pipelines, anything toward its objective) is HANDED to the goal's "
            f"room, not executed here: delegate_goal(to=<session key>), with the "
            "room's session key from the goal above. This conversation "
            "delivers results and reveals — it does not run the goal's "
            "pipelines (2026-09-29: Blender turntable jobs relived a "
            "fail-loop in this chat while the room sat idle).")

    async def _work_block(self, session_key: str, goals: list[dict],
                          background: str) -> str:
        from server.repositories.tasks import TaskRepository
        from server.services.base import local_minute
        from server.services.tasks import tasks_block_lines

        sections: list[str] = []
        if goals:
            sections.append("### Goals this conversation holds\n\n"
                            + self._goals_section(goals).replace("\n### ", "\n#### ")
                            .replace("### ", "#### ", 1))
        try:
            owed = await TaskRepository(self.db).list_for_waiter(session_key, limit=10)
        except Exception:
            logger.warning("work block: waiter promises failed", exc_info=True)
            owed = []
        if owed:
            lines = [f"- {t['id']} (due {local_minute(t['due']) if t['due'] else '—'}) "
                     f"[{t.get('expected_completer') or 'you'}] {t['title']}"
                     for t in owed]
            sections.append(
                "### Owed to this conversation (already recorded)\n\n"
                + "\n".join(lines) +
                "\n\nThese promises are already recorded — do NOT add them "
                "again. Close one with close_goal when it's done or no longer "
                "needed; its result wakes this conversation when someone else "
                "closes it.")
        try:
            from server.repositories.goals import GoalRepository
            suggested = await GoalRepository(self.db).suggestions_for(session_key)
        except Exception:
            logger.warning("work block: suggestions failed", exc_info=True)
            suggested = []
        if suggested:
            lines = [f"- {g['id']}: {g['objective']}" for g in suggested]
            sections.append(
                "### Suggested (offered here, awaiting their answer)\n\n"
                + "\n".join(lines) +
                "\n\nYou offered these. If someone here clearly says yes, call "
                "accept_suggestion(goal_id) — in a group pass owner= (who it's "
                "for). If they decline or it's moot, close_goal(goal_id, "
                "outcome='cancelled', result=<why>). Never accept on your own; "
                "you may gently re-raise one when the conversation invites it.")
        asked = await tasks_block_lines(session_key, self.db)
        if asked:
            sections.append("### Asked of this conversation\n\n" + asked)
        if background:
            sections.append(background.replace("## Running in the background",
                                               "### Running in the background", 1))
        if not sections:
            return ""
        return "## Work\n\n" + "\n\n".join(sections)

    async def _background_block(self, session_key: str) -> str:
        """Work running in the background FOR this conversation right now:
        detached flights (runs) and live subagents. Replaces their old
        bookkeeping goals in the block above."""
        from datetime import datetime, timezone
        from server.repositories.runs import RunRepository
        from server.repositories.subagents import SubagentRepository

        lines: list[str] = []
        now = datetime.now(timezone.utc)

        def _age(started: str | None) -> str:
            try:
                dt = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return f"{max(int((now - dt).total_seconds() // 60), 0)}m"
            except (TypeError, ValueError):
                return "?"

        try:
            for run in await RunRepository(self.db).running_for_session(
                    session_key, kind="flight"):
                lines.append(f"- bg turn {run['id'][:8]} (running {_age(run['started_at'])}): "
                             f"{run['summary']}")
            for sub in await SubagentRepository(self.db).list_for_parent(
                    session_key, status="running", limit=5):
                lines.append(f"- subagent {sub['id'][:8]} (running {_age(sub['created_at'])}): "
                             f"{sub['task_preview']}")
        except Exception:
            logger.warning("background block failed for %s", session_key, exc_info=True)
            return ""
        if not lines:
            return ""
        return ("## Running in the background\n\n" + "\n".join(lines) +
                "\n\nThese speak for themselves when done — don't redo their "
                "work or report them finished. check_subagent / kill_subagent "
                "take the id shown.")
