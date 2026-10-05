"""Unified goal tools (commitments plan Phase 3, 2026-10-05).

Six tools replace the general work surface (create_goal, complete_goal,
list_goals, schedule_goal_wakeup, task_register/complete/fail/cancel,
list_tasks):

  add_goal         — an outcome (gets a room) or a promise (owed by a
                     completer); outcomes take a batch of promise children
  close_goal       — completed | failed | cancelled, idempotent
  list_goals       — outcomes this conversation holds + promises it waits on
                     or owes, one call
  delegate_goal    — a promise owed by another conversation, which is woken
                     with the instruction now
  schedule_goal    — a wakeup tied to a goal (rooms keep goal_continue_now /
                     goal_wait — the loop contract is untouched)
  accept_suggestion — (Phase 4) turn a suggested goal active

Names are verb + 'goal', never bare verbs: 'commit' collides with git
commit, 'settle' with payment settlement (plan review 2026-10-05).

These are thin wrappers over the proven handlers in goal_tools / tasks /
goal_service — no new lifecycle logic. Access mirrors today's gating:
  full   — trusted chats and system wakes: everything
  create — untrusted groups: promises + create outcomes (pinned owner),
           but not close/schedule others' outcomes
  none   — untrusted DMs: promises only

"""

from __future__ import annotations

import json
import logging
from typing import Any

from server.context import AppContext
from server.services.tools import Tool, tool

logger = logging.getLogger(__name__)

ACCESS_LEVELS = ("full", "create", "none")
_CLOSE_STATUSES = ("completed", "failed", "cancelled")


def _parse_children(children: str) -> tuple[list[dict[str, Any]], str | None]:
    """(specs, error). Each child needs a non-empty 'text'."""
    if not children.strip():
        return [], None
    try:
        specs = json.loads(children)
    except json.JSONDecodeError:
        specs = None
    if not isinstance(specs, list) or not specs or not all(
            isinstance(c, dict) and str(c.get("text", "")).strip() for c in specs):
        return [], ("children must be a JSON array of objects with a 'text' "
                    "field (optional: completer, instruction, due_minutes)")
    return specs, None


def make_goal_work_tools(ctx: AppContext, session_key: str, *,
                         access: str = "full") -> list[Tool]:
    from server.services import tasks as task_svc
    from server.services.goal_tools import goal_tool_handlers

    if access not in ACCESS_LEVELS:
        access = "none"
    # Wrapped handlers — the proven implementations, gated below.
    legacy_goal = {t.name: t for t in goal_tool_handlers(ctx, session_key)}
    legacy_task = {t.name: t for t in _legacy_task_tools(ctx, session_key)}

    async def _register_promise(*, text: str, instruction: str,
                                completer: str, due_minutes: float,
                                waiter: str, source_goal_id: str | None) -> dict:
        task = await task_svc.register_task(
            ctx, waiter_session=waiter, title=text, instruction=instruction,
            expected_completer=completer.strip() or None,
            due_minutes=due_minutes or None, source_goal_id=source_goal_id)
        if not task.get("ok", True):
            return task  # refusal carries its reroute guidance
        out = {"ok": True, "goal_id": task["id"], "profile": "promise",
               "due": task["due"]}
        if task.get("steer_note"):
            out["warning"] = task["steer_note"]
        return out

    @tool
    async def add_goal(
        text: str = "",
        profile: str = "outcome",
        label: str = "",
        completer: str = "",
        instruction: str = "",
        due_minutes: float = 0,
        deadline: str = "",
        parent_goal_id: str = "",
        owner: str = "",
        strategy: str = "",
        children: str = "",
    ) -> str:
        """Record something owed — call it whenever you take on work or
        promise to do something ("I'll…", "rebuilding it now"): later turns
        only know what is recorded. Two profiles:

        - profile="outcome" (default): a multi-step objective. It gets its
          own room and runs in rounds. `text` must name the artefact AND
          the proof it's done (see the goal-craft skill). `label` is a
          short free description (research, build, event_plan…).
          `deadline` (ISO 8601 UTC) or `owner` (in a GROUP: who it's for —
          ask if unclear) optional.
        - profile="promise": one thing owed by a single party — you, or
          another conversation (`completer` = its session key, which is
          also woken with `instruction` now). `due_minutes` defaults to 60.

        `children`: JSON array of promises created in the SAME call — one
        per person/item for fan-out work. On a new outcome they hang off
        it; with `parent_goal_id` of an EXISTING goal (e.g. a room
        splitting its own goal) they're added under that goal. E.g.
        [{"text": "Sam's figurine model", "completer": "", "instruction":
        "…", "due_minutes": 1440}]. Use it instead of many separate calls.
        Before paid or irreversible steps, add a child whose completer is
        the owner's conversation asking them to approve.
        """
        profile = (profile or "outcome").strip().lower()
        child_specs, bad = _parse_children(children)
        if bad:
            return json.dumps({"ok": False, "error": bad})

        # Batch children under an EXISTING goal (a room splitting its own
        # objective): parent_goal_id + children, no new goal is created.
        if child_specs and parent_goal_id.strip() and (
                profile == "promise" or not text.strip()):
            from server.repositories.goals import GoalRepository
            parent = await GoalRepository(ctx.db).get(parent_goal_id.strip())
            if parent is None or parent["status"] != "active":
                return json.dumps({"ok": False, "error":
                    f"parent goal {parent_goal_id!r} not found or not active"})
            kids = [await _register_promise(
                        text=str(c["text"]),
                        instruction=str(c.get("instruction") or ""),
                        completer=str(c.get("completer") or ""),
                        due_minutes=float(c.get("due_minutes") or 0),
                        waiter=parent["conversation_id"] or session_key,
                        source_goal_id=parent["id"])
                    for c in child_specs]
            return json.dumps({"ok": all(k.get("ok") for k in kids),
                               "goal_id": parent["id"], "children": kids})

        if not text.strip():
            return json.dumps({"ok": False, "error":
                "text is required (to add children to an existing goal, "
                "pass its parent_goal_id with children and no text)"})
        if profile == "promise":
            if child_specs:
                return json.dumps({"ok": False, "error":
                    "to add children to an existing goal pass its "
                    "parent_goal_id; to create a new goal with children use "
                    "profile='outcome'"})
            return json.dumps(await _register_promise(
                text=text, instruction=instruction, completer=completer,
                due_minutes=due_minutes, waiter=session_key,
                source_goal_id=parent_goal_id.strip() or None))
        if profile != "outcome":
            return json.dumps({"ok": False,
                               "error": "profile must be 'outcome' or 'promise'"})
        if access == "none":
            return json.dumps({"ok": False, "error":
                "this conversation can record promises (profile='promise') "
                "but not create outcome goals"})

        created = json.loads(await legacy_goal["create_goal"].handler(
            objective=text, kind=label.strip() or "task", deadline=deadline,
            parent_goal_id=parent_goal_id, strategy=strategy, owner=owner))
        if not created.get("ok"):
            return json.dumps(created)
        goal_id = created.get("goal_id")
        if child_specs and goal_id:
            from server.repositories.goals import GoalRepository
            goal = await GoalRepository(ctx.db).get(goal_id)
            waiter = (goal or {}).get("conversation_id") or session_key
            kids = []
            for spec in child_specs:
                kids.append(await _register_promise(
                    text=str(spec["text"]),
                    instruction=str(spec.get("instruction") or ""),
                    completer=str(spec.get("completer") or ""),
                    due_minutes=float(spec.get("due_minutes") or 0),
                    waiter=waiter, source_goal_id=goal_id))
            created["children"] = kids
        created["profile"] = "outcome"
        return json.dumps(created)

    @tool
    async def close_goal(goal_id: str, outcome: str = "completed",
                         result: str = "") -> str:
        """Close a goal: outcome = completed | failed | cancelled, with the
        result (what was achieved, or why it failed / was withdrawn). Works
        for promises (yours, or ones you were asked to complete — e.g.
        prm-a3f2 or just a3f2) and for outcome goals. Closing something
        already closed is a harmless no-op — never retry it.

        Fail IMMEDIATELY (outcome='failed') when this conversation lacks
        what the work needs (a tool, a channel, a contact) — that reroutes
        it; never substitute a different channel."""
        outcome = (outcome or "completed").strip().lower()
        if outcome not in _CLOSE_STATUSES:
            return json.dumps({"ok": False, "error":
                f"outcome must be one of {', '.join(_CLOSE_STATUSES)}"})
        from server.repositories.tasks import TaskRepository
        repo = TaskRepository(ctx.db)
        promise = await repo.get(goal_id.strip()) or await repo.get_by_short_id(goal_id)
        if promise is not None:
            res = await task_svc.settle_task(
                ctx, promise["id"], to_status=outcome,
                result=result if outcome != "failed" else None,
                error=result if outcome == "failed" else None,
                completed_by=f"conversation:{session_key}")
            if not res.get("ok") and res.get("status"):
                return json.dumps({"ok": True, "goal_id": promise["id"],
                                   "note": f"already {res['status']} — nothing to do"})
            res.pop("task_id", None)
            return json.dumps({**res, "goal_id": promise["id"]})

        from server.repositories.goals import GoalRepository
        sug = await GoalRepository(ctx.db).get(goal_id.strip())
        if sug is not None and sug["kind"] == "suggestion":
            return json.dumps(await _decline_suggestion(sug, result))

        if access != "full":
            return json.dumps({"ok": False, "error":
                "only the goal's owner conversation can close an outcome goal "
                "— report the result here instead"})
        goal = await GoalRepository(ctx.db).get(goal_id.strip())
        if goal is None:
            return json.dumps({"ok": False, "error": f"goal {goal_id!r} not found"})
        if goal["status"] != "active":
            return json.dumps({"ok": True, "goal_id": goal["id"],
                               "note": f"already {goal['status']} — nothing to do"})
        if outcome == "completed":
            return await legacy_goal["complete_goal"].handler(
                goal_id=goal["id"], result=result)
        from server.services.goal_service import settle_goal
        moved = await settle_goal(ctx, goal["id"], status=outcome,
                                  result=result or outcome)
        return json.dumps({"ok": bool(moved), "goal_id": goal["id"],
                           "status": outcome})

    @tool
    async def list_goals() -> str:
        """Everything owed around this conversation, in one call: outcome
        goals it holds (and related ones from conversations sharing a
        participant), promises it WAITS on, and promises it is expected to
        COMPLETE for someone else."""
        out: dict[str, Any] = {"ok": True}
        if access != "none":
            outcomes = json.loads(await legacy_goal["list_goals"].handler())
            out["outcomes"] = outcomes.get("goals", outcomes)
        promises = json.loads(await legacy_task["list_tasks"].handler(status="pending"))
        def _as_goal(item: dict) -> dict:
            # One id vocabulary for the model: promises are goals.
            return {("goal_id" if k == "task_id" else k): v for k, v in item.items()}
        out["waiting_on"] = [_as_goal(i) for i in promises.get("waiting_on", [])]
        out["expected_of_you"] = [_as_goal(i) for i in promises.get("expected_of_you", [])]
        return json.dumps(out)

    @tool
    async def delegate_goal(text: str, to: str, instruction: str = "",
                            due_minutes: float = 0,
                            parent_goal_id: str = "") -> str:
        """Hand work to another conversation: records a promise it owes
        this one AND wakes it now with `instruction`. `to` is that
        conversation's session key (find it with find_session). When it
        closes the promise, this conversation is woken with the result.
        Per-person work goes to the PERSON'S DM, never a group (a group can
        only reply in the group). due_minutes defaults to 60."""
        if not to.strip():
            return json.dumps({"ok": False, "error":
                "`to` must be a conversation session key (use find_session)"})
        return json.dumps(await _register_promise(
            text=text, instruction=instruction or text, completer=to,
            due_minutes=due_minutes, waiter=session_key,
            source_goal_id=parent_goal_id.strip() or None))

    async def _decline_suggestion(sug: dict, reason: str) -> dict:
        if sug["origin_conversation_id"] != session_key:
            return {"ok": False, "error": "that suggestion was offered in another conversation"}
        from server.repositories.goals import GoalRepository
        if sug["status"] != "suggested":
            return {"ok": True, "goal_id": sug["id"],
                    "note": f"already {sug['status']} — nothing to do"}
        await GoalRepository(ctx.db).settle_suggestion(
            sug["id"], status="declined", result=reason or "declined")
        if sug.get("external_ref"):
            from server.services.dream.models import Evidence
            from server.services.dream.store import DreamStore
            await DreamStore(ctx).set_plan_status(
                sug["external_ref"], "dismissed",
                evidence=Evidence(kind="dismissed", note=reason or "declined in chat"))
        return {"ok": True, "goal_id": sug["id"], "status": "declined"}

    @tool
    async def accept_suggestion(goal_id: str, owner: str = "") -> str:
        """Someone in this conversation said YES to a suggestion you offered
        (listed under "Suggested" in the Work block): this creates the real
        goal and starts work on it. Only call it after a human here has
        clearly accepted — never on your own initiative. In a GROUP, pass
        `owner` (who it's for — ask if unclear). If they decline instead,
        close_goal(goal_id, outcome='cancelled', result=<their reason>)."""
        from server.repositories.goals import GoalRepository
        repo = GoalRepository(ctx.db)
        sug = await repo.get(goal_id.strip())
        if sug is None or sug["kind"] != "suggestion":
            return json.dumps({"ok": False, "error": f"no suggestion {goal_id!r}"})
        if sug["origin_conversation_id"] != session_key:
            return json.dumps({"ok": False, "error":
                "that suggestion was offered in another conversation"})
        if sug["status"] != "suggested":
            return json.dumps({"ok": True, "goal_id": sug["result"] or sug["id"],
                               "note": f"already {sug['status']} — nothing to do"})
        created = json.loads(await legacy_goal["create_goal"].handler(
            objective=sug["objective"], kind="task", owner=owner))
        if not created.get("ok"):
            return json.dumps(created)  # e.g. a group with no owner named
        new_id = created["goal_id"]
        await repo.settle_suggestion(sug["id"], status="accepted", result=new_id)
        if sug.get("external_ref"):
            from server.services.dream.store import DreamStore
            store = DreamStore(ctx)
            await store.set_plan_task_id(sug["external_ref"], new_id)
            await store.set_plan_status(sug["external_ref"], "actioned")
        return json.dumps({"ok": True, "goal_id": new_id, "accepted": sug["id"]})

    tools = [add_goal, close_goal, list_goals, delegate_goal, accept_suggestion]

    if access == "full":
        @tool
        async def schedule_goal(goal_id: str, not_before: str,
                                note: str = "") -> str:
            """Wake about an outcome goal later: `not_before` is an ISO 8601
            UTC time (e.g. a reminder before an event). The goal's working
            conversation is woken with the note and the goal's state. The
            wakeup is dropped automatically if the goal closes first. (Inside
            a goal room, continue with goal_continue_now / goal_wait.)"""
            return await legacy_goal["schedule_goal_wakeup"].handler(
                goal_id=goal_id, not_before=not_before, note=note)
        tools.append(schedule_goal)

    # State + templates stay as they are (outcome bookkeeping).
    keep = (("update_goal", "update_goal_state",
             "list_goal_templates", "instantiate_goal_template")
            if access == "full" else ())
    tools.extend(legacy_goal[name] for name in keep if name in legacy_goal)
    return tools


def _legacy_task_tools(ctx: AppContext, session_key: str) -> list[Tool]:
    from server.services.tasks import promise_tool_handlers
    return promise_tool_handlers(ctx, session_key)
