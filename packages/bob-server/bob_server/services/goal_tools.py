"""Goal tools for LLM function calling (Bob3 Phase V).

Goal mutations are emitted as effects like everything else: recorded before
execution, idempotent, retried by the pump on crash.
"""

from __future__ import annotations

import json
import logging
from uuid import uuid4

from bob_server.context import AppContext
from bob_server.services.tools import tool

logger = logging.getLogger(__name__)


def _register_goal_executors() -> None:
    from bob_server.services import effects as effects_svc
    from bob_server.services import goal_service

    async def _exec_create(ctx, payload):
        goal = await goal_service.create_goal(
            ctx,
            conversation_id=payload["conversation_id"],
            objective=payload["objective"],
            origin_conversation_id=payload.get("origin_conversation_id"),
            kind=payload.get("kind", "task"),
            deadline=payload.get("deadline"),
            external_ref=payload.get("external_ref"),
            goal_id=payload.get("goal_id"),
        )
        return goal["id"]

    async def _exec_revise(ctx, payload):
        from bob_server.repositories.goals import GoalRepository
        ok = await GoalRepository(ctx.db).revise(
            payload["goal_id"],
            expected_version=payload["expected_version"],
            objective=payload.get("objective"),
            progress=payload.get("progress"),
            deadline=payload.get("deadline"),
        )
        if not ok:
            raise RuntimeError("stale version or goal not active")
        return payload["goal_id"]

    async def _exec_complete(ctx, payload):
        ok = await goal_service.settle_goal(
            ctx, payload["goal_id"],
            status=payload.get("status", "completed"),
            result=payload.get("result", ""),
        )
        return payload["goal_id"] if ok else None

    effects_svc.register_executor("goal_create", _exec_create)
    effects_svc.register_executor("goal_revise", _exec_revise)
    effects_svc.register_executor("goal_complete", _exec_complete)


_register_goal_executors()


def make_goal_tools(ctx: AppContext, session_key: str) -> list:
    """Goal tools for a conversation: create, update, complete, list."""

    @tool
    async def create_goal(
        objective: str,
        deadline: str = "",
    ) -> str:
        """Create a goal this conversation is working toward. The objective
        describes the concrete outcome needed. An optional deadline
        (ISO 8601 UTC timestamp) schedules a wakeup — if the goal is still
        open at that time you will be woken to follow up."""
        from bob_server.services.effects import emit_and_deliver

        goal_id = str(uuid4())
        result = await emit_and_deliver(
            ctx, kind="goal_create",
            idempotency_key=f"goal_create:{goal_id}",
            payload={
                "conversation_id": session_key,
                "objective": objective,
                "deadline": deadline or None,
                "goal_id": goal_id,
            })
        if not result.get("ok"):
            return json.dumps({"ok": False, "error": result.get("error", "failed")})
        return json.dumps({"ok": True, "goal_id": goal_id})

    @tool
    async def update_goal(
        goal_id: str,
        progress: str,
        expected_version: int,
    ) -> str:
        """Record progress on an active goal. expected_version must match the
        goal's current version (from list_goals) — a stale update is rejected
        so an outdated strategy never overwrites a newer one."""
        from bob_server.services.effects import emit_and_deliver

        result = await emit_and_deliver(
            ctx, kind="goal_revise",
            idempotency_key=f"goal_revise:{goal_id}:{expected_version}",
            payload={"goal_id": goal_id, "progress": progress,
                     "expected_version": expected_version})
        if not result.get("ok"):
            return json.dumps({"ok": False, "error": result.get("error", "failed")})
        return json.dumps({"ok": True, "goal_id": goal_id})

    @tool
    async def complete_goal(goal_id: str, result: str) -> str:
        """Complete an active goal with its outcome. If the goal was created
        on behalf of another conversation, that conversation is woken with the
        result so it can relay it."""
        from bob_server.services.effects import emit_and_deliver

        outcome = await emit_and_deliver(
            ctx, kind="goal_complete",
            idempotency_key=f"goal_complete:{goal_id}",
            payload={"goal_id": goal_id, "result": result})
        if not outcome.get("ok"):
            return json.dumps({"ok": False, "error": outcome.get("error", "failed")})
        if not outcome.get("external_result_id") and not outcome.get("duplicate"):
            return json.dumps({"ok": False, "error": "goal already settled or not found"})
        return json.dumps({"ok": True, "goal_id": goal_id})

    @tool
    async def list_goals() -> str:
        """List this conversation's active goals (id, objective, progress,
        version, deadline)."""
        from bob_server.repositories.goals import GoalRepository

        rows = await GoalRepository(ctx.db).list_active(conversation_id=session_key)
        return json.dumps({
            "ok": True,
            "goals": [
                {"goal_id": r["id"], "objective": r["objective"],
                 "progress": r["progress"], "version": r["version"],
                 "deadline": r["deadline"], "kind": r["kind"],
                 "created_at": r["created_at"]}
                for r in rows
            ],
        })

    return [create_goal, update_goal, complete_goal, list_goals]
