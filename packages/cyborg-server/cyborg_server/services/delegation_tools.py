"""Delegation tools — let Cyborg's LLM manage skill creation delegations to Claude Code."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from cyborg_server.services.tools import tool

if TYPE_CHECKING:
    from cyborg_server.context import AppContext

logger = logging.getLogger(__name__)


def make_delegation_tools(ctx: AppContext, session_key: str) -> list:
    """Create delegation tools for a trusted session."""

    @tool
    async def delegate_to_claude(user_story: str) -> str:
        """Submit a user story to Claude Code for skill planning.
        Describe the capability you need as a clear user story.
        Returns a plan for your review — call implement_delegation to proceed."""
        from cyborg_server.services.skill_developer_service import SkillDeveloperService

        svc = SkillDeveloperService(ctx)
        result = await svc.plan_skill(user_story, session_key)
        return json.dumps(result)

    @tool
    async def implement_delegation(delegation_id: str) -> str:
        """Approve and execute a delegation plan. Claude Code will create the skill files.
        Only call this after reviewing the plan from delegate_to_claude."""
        from cyborg_server.services.skill_developer_service import SkillDeveloperService

        svc = SkillDeveloperService(ctx)
        result = await svc.implement_skill(delegation_id)
        return json.dumps(result)

    @tool
    async def reject_delegation(delegation_id: str, reason: str) -> str:
        """Reject a delegation plan with feedback."""
        from cyborg_server.services.skill_developer_service import SkillDeveloperService

        svc = SkillDeveloperService(ctx)
        result = await svc.reject_skill(delegation_id, reason)
        return json.dumps(result)

    @tool
    async def list_delegations(status: str = "") -> str:
        """List skill delegations, optionally filtered by status.
        Valid statuses: planning, plan_ready, implementing, completed, failed, rejected."""
        query = "SELECT id, status, substr(user_story, 1, 100) as user_story_preview, created_at FROM skill_delegations"
        params: list[str] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT 20"
        rows = await ctx.db.fetch_all(query, tuple(params))
        results = [
            {
                "id": row["id"],
                "status": row["status"],
                "user_story": row["user_story_preview"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
        return json.dumps(results)

    return [delegate_to_claude, implement_delegation, reject_delegation, list_delegations]
