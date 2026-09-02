"""Goal tools for LLM function calling (Bob3 Phase V, extended Bob Events §1.5).

Goal mutations are emitted as effects like everything else: recorded before
execution, idempotent, retried by the pump on crash.

Bob Events extensions: ``create_goal`` takes kind/parent/strategy (validated
against the v2 strategy schema), ``update_goal_state`` does CAS
read-modify-write on the strategy worksheet, ``list_goals`` includes the
state summary and children, and ``schedule_goal_wakeup`` books an arbitrary
ISO-time wakeup against a goal (reminders).
"""

from __future__ import annotations

import json
import logging
from uuid import uuid4

from server.context import AppContext
from server.services.tools import tool

logger = logging.getLogger(__name__)


def _validate_strategy_payload(strategy_json: str) -> dict:
    """Parse + validate a strategy JSON string from the LLM against the v2
    schema (partial input allowed; defaults fill). Returns {"state": …} or
    {"error": reason} for the caller to relay."""
    from server.services.goal_state_service import GoalStrategy

    try:
        raw = json.loads(strategy_json or "{}")
    except json.JSONDecodeError as exc:
        return {"error": f"strategy is not valid JSON: {exc}"}
    if not isinstance(raw, dict):
        return {"error": "strategy must be a JSON object"}
    try:
        state = GoalStrategy.model_validate({**raw, "v": 2})
    except Exception as exc:  # ValidationError with details
        return {"error": f"strategy failed schema validation: {exc}"}
    return {"state": state.model_dump(mode="json", exclude_none=True)}


def _register_goal_executors() -> None:
    from server.services import effects as effects_svc
    from server.services import goal_service

    async def _exec_create(ctx, payload):
        goal = await goal_service.create_goal(
            ctx,
            conversation_id=payload["conversation_id"],
            objective=payload["objective"],
            origin_conversation_id=payload.get("origin_conversation_id"),
            kind=payload.get("kind", "task"),
            strategy=payload.get("strategy"),
            deadline=payload.get("deadline"),
            external_ref=payload.get("external_ref"),
            parent_goal_id=payload.get("parent_goal_id"),
            goal_id=payload.get("goal_id"),
        )
        return goal["id"]

    async def _exec_revise(ctx, payload):
        from server.repositories.goals import GoalRepository
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

    async def _exec_state_write(ctx, payload):
        from server.repositories.goals import GoalRepository
        ok = await GoalRepository(ctx.db).revise(
            payload["goal_id"],
            expected_version=payload["expected_version"],
            strategy_json=payload["strategy_json"],
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

    async def _exec_wakeup_schedule(ctx, payload):
        from server.repositories.goals import GoalRepository
        from server.repositories.wakeups import WakeupRepository

        repo = GoalRepository(ctx.db)
        goal = await repo.get(payload["goal_id"])
        if goal is None or goal["status"] != "active":
            return None  # moot: goal gone/settled
        await WakeupRepository(ctx.db).schedule(
            conversation_id=await goal_service._wakeup_target(repo, goal),
            not_before=payload["not_before"],
            goal_id=goal["id"],
            payload={"note": payload.get("note", ""), "scheduled_by": "tool"},
        )
        return goal["id"]

    effects_svc.register_executor("goal_create", _exec_create)
    effects_svc.register_executor("goal_revise", _exec_revise)
    effects_svc.register_executor("goal_state_write", _exec_state_write)
    effects_svc.register_executor("goal_complete", _exec_complete)
    effects_svc.register_executor("goal_wakeup_schedule", _exec_wakeup_schedule)


_register_goal_executors()


def make_goal_tools(ctx: AppContext, session_key: str) -> list:
    """Goal tools for a conversation: create, update, update state,
    schedule wakeup, complete, list."""

    @tool
    async def create_goal(
        objective: str,
        kind: str = "task",
        deadline: str = "",
        parent_goal_id: str = "",
        strategy: str = "",
    ) -> str:
        """Create a goal this conversation is working toward.

        - objective: the concrete outcome needed.
        - kind: task | outreach | subagent | call | email_thread | negotiate | event_plan.
        - deadline (optional, ISO 8601 UTC): schedules a wakeup — if the goal
          is still open then, the ROOT goal's working conversation is woken to
          follow up.
        - parent_goal_id (optional): make this a sub-goal. Its results roll up
          into the parent's state instead of waking anyone directly.
        - strategy (optional, JSON): the v2 state worksheet —
          {"plan": str, "known": [str], "open_questions": [str],
           "next_actions": [{"action": str, "due": str}],
           "refs": {"entities": [str], "claims": [str]}}
          plus scenario data (e.g. decision rules)."""
        from server.services.effects import emit_and_deliver

        strategy_payload: dict | None = None
        if strategy.strip():
            checked = _validate_strategy_payload(strategy)
            if "error" in checked:
                return json.dumps({"ok": False, "error": checked["error"]})
            strategy_payload = checked["state"]
        parent = parent_goal_id.strip() or None
        if parent:
            from server.repositories.goals import GoalRepository
            parent_goal = await GoalRepository(ctx.db).get(parent)
            if parent_goal is None or parent_goal["status"] != "active":
                return json.dumps({"ok": False,
                                   "error": "parent goal not found or not active"})

        goal_id = str(uuid4())
        result = await emit_and_deliver(
            ctx, kind="goal_create",
            idempotency_key=f"goal_create:{goal_id}",
            payload={
                "conversation_id": session_key,
                "objective": objective,
                "kind": kind or "task",
                "deadline": deadline or None,
                "parent_goal_id": parent,
                "strategy": strategy_payload,
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
        from server.services.effects import emit_and_deliver

        result = await emit_and_deliver(
            ctx, kind="goal_revise",
            idempotency_key=f"goal_revise:{goal_id}:{expected_version}",
            payload={"goal_id": goal_id, "progress": progress,
                     "expected_version": expected_version})
        if not result.get("ok"):
            return json.dumps({"ok": False, "error": result.get("error", "failed")})
        return json.dumps({"ok": True, "goal_id": goal_id})

    @tool
    async def update_goal_state(
        goal_id: str,
        state: str,
        expected_version: int,
    ) -> str:
        """Replace an active goal's state worksheet (the strategy JSON from
        list_goals, updated). Provide the COMPLETE updated state —
        {"plan": str, "known": [str], "open_questions": [str],
         "next_actions": [{"action": str, "due": str}],
         "refs": {"entities": [str], "claims": [str]}} — preserving keys you
        are not changing. expected_version must match (from list_goals)."""
        from server.services.effects import emit_and_deliver

        checked = _validate_strategy_payload(state)
        if "error" in checked:
            return json.dumps({"ok": False, "error": checked["error"]})

        result = await emit_and_deliver(
            ctx, kind="goal_state_write",
            idempotency_key=f"goal_state_write:{goal_id}:{expected_version}",
            payload={"goal_id": goal_id,
                     "strategy_json": json.dumps(checked["state"], ensure_ascii=False),
                     "expected_version": expected_version})
        if not result.get("ok"):
            return json.dumps({"ok": False, "error": result.get("error", "failed")})
        if result.get("duplicate"):
            # Same (goal, version) key seen before. If the goal has moved past
            # this version the earlier write applied — this attempt is stale
            # (a different payload reusing the version). If the version still
            # matches, the earlier effect is merely pending: idempotent ok.
            from server.repositories.goals import GoalRepository
            current = await GoalRepository(ctx.db).get(goal_id)
            if current is None or current["version"] > expected_version:
                return json.dumps({"ok": False,
                                   "error": "stale version: goal already at a newer "
                                            "version (re-read with list_goals)"})
        return json.dumps({"ok": True, "goal_id": goal_id})

    @tool
    async def schedule_goal_wakeup(
        goal_id: str,
        not_before: str,
        note: str = "",
    ) -> str:
        """Schedule a wakeup tied to a goal at an ISO 8601 UTC timestamp
        (e.g. reminders before an event). When it fires, the ROOT goal's
        working conversation is woken with the note and the goal's current
        state. The wakeup is cancelled automatically if the goal settles."""
        from server.services.effects import emit_and_deliver

        result = await emit_and_deliver(
            ctx, kind="goal_wakeup_schedule",
            idempotency_key=f"goal_wakeup:{goal_id}:{uuid4()}",
            payload={"goal_id": goal_id, "not_before": not_before, "note": note})
        if not result.get("ok"):
            return json.dumps({"ok": False, "error": result.get("error", "failed")})
        return json.dumps({"ok": True, "goal_id": goal_id, "not_before": not_before})

    @tool
    async def list_goal_templates() -> str:
        """List available goal templates — playbooks instantiable as a full
        goal tree (children + decision rules). Shows each template's
        description and required params."""
        from server.services.goal_templates import load_templates

        templates = load_templates(ctx.settings.config_dir)
        return json.dumps({"ok": True, "templates": [
            {"name": name, "description": t.get("description", ""),
             "params": t.get("params", [])}
            for name, t in sorted(templates.items())]})

    @tool
    async def instantiate_goal_template(
        template: str,
        params_json: str = "{}",
    ) -> str:
        """Instantiate a goal template as a goal tree worked by THIS
        conversation (root + children with decision rules; the named group
        conversation is registered as a holder so replies given there route
        back).

        params_json example for team-event:
        {"event_name": "team lunch", "group_name": "AI doom",
         "group_session_key": "agent:main:whatsapp:group:<groupid>",
         "decide_by": "2026-09-05T17:00:00+00:00"}
        Adapt the created tree afterwards with the other goal tools."""
        from server.services.goal_templates import instantiate_template

        try:
            params = json.loads(params_json or "{}")
        except json.JSONDecodeError as exc:
            return json.dumps({"ok": False, "error": f"params_json invalid: {exc}"})
        if not isinstance(params, dict):
            return json.dumps({"ok": False, "error": "params_json must be an object"})
        try:
            result = await instantiate_template(
                ctx, template_name=template, session_key=session_key,
                params={k: str(v) for k, v in params.items()})
        except ValueError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        return json.dumps({"ok": True, **result})

    @tool
    async def complete_goal(goal_id: str, result: str) -> str:
        """Complete an active goal with its outcome. If the goal was created
        on behalf of another conversation and has no parent, that
        conversation is woken with the result; a child goal's result rolls up
        into its parent's state instead."""
        from server.services.effects import emit_and_deliver

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
        """List this conversation's active goals: id, objective, kind,
        progress, version, deadline, parent/children, and the state summary
        (plan, known, open questions, next actions, entity refs)."""
        from server.repositories.goals import GoalRepository
        from server.services.goal_state_service import parse_strategy

        repo = GoalRepository(ctx.db)
        rows = await repo.list_active(conversation_id=session_key)
        goals = []
        for r in rows:
            state = parse_strategy(r)
            goals.append({
                "goal_id": r["id"], "objective": r["objective"],
                "kind": r["kind"], "progress": r["progress"],
                "version": r["version"], "deadline": r["deadline"],
                "parent_goal_id": r.get("parent_goal_id"),
                "plan": state.plan[:200],
                "known": state.known, "open_questions": state.open_questions,
                "next_actions": [na.model_dump() for na in state.next_actions],
                "refs": state.refs.model_dump(),
                "children": [c["id"] for c in await repo.children_of(r["id"], status="active")],
            })
        return json.dumps({"ok": True, "goals": goals})

    return [create_goal, update_goal, update_goal_state,
            schedule_goal_wakeup, list_goal_templates,
            instantiate_goal_template, complete_goal, list_goals]
