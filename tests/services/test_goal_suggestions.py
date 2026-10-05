"""Dream suggestions as goals (commitments plan Phase 4, 2026-10-05).

A dream offer announced in a chat is a suggestion goal (kind 'suggestion',
status 'suggested' — invisible to goal sweepers). The chat's Work block
lists it; accept_suggestion creates the real goal and marks the dream plan
actioned; close_goal declines (plan dismissed); the dream closing/expiring
the plan expires the suggestion. Only the chat it was offered in can act.
"""

from __future__ import annotations

import json

from server.repositories.goals import GoalRepository
from server.services.context_assembler import ContextAssembler
from server.services.dream.models import PlanCandidate
from server.services.dream.store import DreamStore
from server.services.goal_work_tools import make_goal_work_tools

DM = "agent:main:whatsapp:dm:61400000003"


async def _plan_and_suggestion(ctx):
    store = DreamStore(ctx)
    run = await store.create_run(trigger="manual", window_start="2026-10-01T00:00:00Z",
                                 window_end="2026-10-05T00:00:00Z", model="test")
    plan_id = await store.insert_plan(PlanCandidate(
        title="Book the Saturday BBQ venue",
        what_was_discussed="the group wants a BBQ but nobody booked",
        proposed_action="call two venues", assistance_method="phone calls"),
        run_id=run, status="approved", session_key=DM)
    sug = await GoalRepository(ctx.db).create_suggestion(
        session_key=DM, plan_id=plan_id, text="Book the Saturday BBQ venue")
    return store, plan_id, sug


def _tools(ctx, key=DM):
    return {t.name: t for t in make_goal_work_tools(ctx, key, access="full")}


async def test_suggestion_is_listed_and_invisible_to_sweepers(ctx):
    _, _, sug = await _plan_and_suggestion(ctx)
    assert sug["status"] == "suggested" and sug["kind"] == "suggestion"
    active = await GoalRepository(ctx.db).list_active(limit=500)
    assert sug["id"] not in {g["id"] for g in active}
    block = await ContextAssembler(ctx).goals_block(DM)
    assert "### Suggested" in block and sug["id"] in block


async def test_accept_creates_goal_and_actions_plan(ctx):
    store, plan_id, sug = await _plan_and_suggestion(ctx)
    r = json.loads(await _tools(ctx)["accept_suggestion"].handler(goal_id=sug["id"]))
    assert r["ok"], r
    goal = await GoalRepository(ctx.db).get(r["goal_id"])
    assert goal["status"] == "active" and goal["kind"] != "suggestion"
    assert (await GoalRepository(ctx.db).get(sug["id"]))["status"] == "accepted"
    plan = await store.get_plan(plan_id)
    assert plan["status"] == "actioned" and plan["task_id"] == r["goal_id"]
    again = json.loads(await _tools(ctx)["accept_suggestion"].handler(goal_id=sug["id"]))
    assert again["ok"] and "already accepted" in again["note"]


async def test_decline_via_close_goal_dismisses_plan(ctx):
    store, plan_id, sug = await _plan_and_suggestion(ctx)
    r = json.loads(await _tools(ctx)["close_goal"].handler(
        goal_id=sug["id"], outcome="cancelled", result="already booked it"))
    assert r["ok"] and r["status"] == "declined"
    assert (await store.get_plan(plan_id))["status"] == "dismissed"


async def test_only_the_offering_chat_can_act(ctx):
    _, _, sug = await _plan_and_suggestion(ctx)
    other = _tools(ctx, "agent:main:whatsapp:dm:61400000999")
    r = json.loads(await other["accept_suggestion"].handler(goal_id=sug["id"]))
    assert not r["ok"] and "another conversation" in r["error"]


async def test_dream_expiry_expires_the_suggestion(ctx):
    store, plan_id, sug = await _plan_and_suggestion(ctx)
    await store.set_plan_status(plan_id, "expired")
    assert (await GoalRepository(ctx.db).get(sug["id"]))["status"] == "expired"
    block = await ContextAssembler(ctx).goals_block(DM)
    assert sug["id"] not in block
