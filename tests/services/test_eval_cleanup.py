"""Eval cleanup must not leak live work (2026-10-05).

A model in an eval turn created a room goal with a production-shaped uuid;
cleanup matched only the eval id prefix, so the goal lived on and its room
woke on the live pump. Promise backstops (task_due) key on payload.task_id,
so deleting the promise row orphaned them too.
"""

from __future__ import annotations

from server.repositories.conversations import ConversationRepository
from server.repositories.goals import GoalRepository
from server.repositories.wakeups import WakeupRepository
from server.services import tasks as task_svc

PROD = "agent:main:whatsapp:dm:61400000003"
EVAL = "eval:commit:c1-dm"


async def _status(ctx, wid):
    row = await ctx.db.fetch_one("SELECT status FROM wakeups WHERE id = ?", (wid,))
    return row["status"] if row else None


async def test_eval_born_goals_and_orphan_backstops_go(ctx):
    goals = GoalRepository(ctx.db)
    prod_cid = (await ConversationRepository(ctx.db).ensure(PROD))["id"]
    eval_cid = (await ConversationRepository(ctx.db).ensure(EVAL))["id"]
    prod = await goals.create(conversation_id=prod_cid, objective="real work")
    leaked = await goals.create(conversation_id=eval_cid, origin_conversation_id=eval_cid,
                                objective="model-created in an eval turn")
    child = await goals.create(conversation_id=eval_cid, objective="its child",
                               parent_goal_id=leaked["id"])

    prod_task = await task_svc.register_task(ctx, waiter_session=PROD,
                                             title="real promise", due_minutes=60)
    eval_task = await task_svc.register_task(ctx, waiter_session=EVAL,
                                             title="eval promise", due_minutes=60)
    wakes = WakeupRepository(ctx.db)
    prod_due = await ctx.db.fetch_one(
        "SELECT id FROM wakeups WHERE json_extract(payload_json, '$.task_id') = ?",
        (prod_task["id"],))

    removed = await goals.delete_eval_goals()
    assert {leaked["id"], child["id"]} <= set(removed)
    assert await goals.get(prod["id"]) is not None
    assert await goals.get(leaked["id"]) is None

    await ctx.db.execute("DELETE FROM goals WHERE id = ?", (eval_task["id"],))
    await wakes.delete_eval_wakeups()
    orphan = await ctx.db.fetch_all(
        "SELECT status FROM wakeups WHERE status = 'scheduled' AND "
        "json_extract(payload_json, '$.task_id') = ?", (eval_task["id"],))
    assert not orphan
    assert await _status(ctx, prod_due["id"]) == "scheduled"
