"""Attach mechanisms to the goal they serve (2026-10-09 Night Watch).

The creating group turn wrote two routines for the campaign; the goal's
room never knew they existed and built a second ad mechanism on top.
Routines and background jobs created for a goal now leave a line in the
goal's state (the append-only known-list every round reads). Inside a
goal room the link is implicit.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from server.context import AppContext

logger = logging.getLogger(__name__)


async def resolve_goal_id(ctx: AppContext, session_key: str, goal_id: str = "") -> str:
    """Explicit goal_id wins; a goal room's own goal is implied."""
    gid = (goal_id or "").strip()
    if gid:
        return gid
    if (session_key or "").startswith("agent:goal-"):
        from server.services.goal_rooms import goal_for_room
        goal = await goal_for_room(ctx, session_key)
        return goal["id"] if goal else ""
    return ""


async def link_mechanism(ctx: AppContext, session_key: str, goal_id: str,
                         line: str) -> str:
    """Record ``line`` on the goal. Returns a short note for the tool
    result ('' when there is nothing to link)."""
    gid = await resolve_goal_id(ctx, session_key, goal_id)
    if not gid:
        return ""
    from server.repositories.goals import GoalRepository
    repo = GoalRepository(ctx.db)
    goal = await repo.get(gid)
    if goal is None or goal["status"] != "active":
        return f" (not linked: goal {gid} is not active)"
    try:
        await repo.append_known_line(gid, f"[mechanism] {line}")
    except Exception:
        logger.warning("goal %s: mechanism link failed", gid, exc_info=True)
        return " (goal link failed)"
    return f" (linked to goal {gid[:8]} — it shows in the goal's state)"
