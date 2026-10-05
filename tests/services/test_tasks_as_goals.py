"""Tasks are promise goals (commitments plan Phase 2, 2026-10-05).

Pins: a registered task is a goals row (profile/kind 'promise', status
'pending', id 'task-…'); settle is still CAS-once; and no goal sweeper or
prompt block that works on outcomes ever sees a promise (they scan status
'active' only — the isolation-by-design the migration relies on).
"""

from __future__ import annotations

from server.repositories.goals import GoalRepository
from server.repositories.tasks import TaskRepository
from server.services import tasks as task_svc
from server.services.context_assembler import ContextAssembler

WAITER = "agent:main:whatsapp:dm:61400000001"


async def test_task_is_a_promise_goal(ctx):
    task = await task_svc.register_task(
        ctx, waiter_session=WAITER, title="send the slides", due_minutes=30)
    row = await GoalRepository(ctx.db).get(task["id"])
    assert task["id"].startswith("prm-")
    assert row["kind"] == "promise" and row["profile"] == "promise"
    assert row["status"] == "pending"
    assert row["objective"] == "send the slides"
    assert row["origin_conversation_id"] == WAITER

    # task-shaped reads still work under the old field names
    t = await TaskRepository(ctx.db).get(task["id"])
    assert t["title"] == "send the slides" and t["waiter_session"] == WAITER
    assert (await TaskRepository(ctx.db).get_by_short_id(task["id"].removeprefix("prm-")[:4]))["id"] == task["id"]


async def test_settle_is_cas_once(ctx):
    repo = TaskRepository(ctx.db)
    task = await task_svc.register_task(
        ctx, waiter_session=WAITER, title="once", due_minutes=30)
    first = await repo.settle(task["id"], to_status="completed",
                              result="done", completed_by="test")
    assert first and first["status"] == "completed"
    assert await repo.settle(task["id"], to_status="failed",
                             error="late", completed_by="test") is None


async def test_goal_sweepers_never_see_promises(ctx):
    task = await task_svc.register_task(
        ctx, waiter_session=WAITER, title="invisible to sweepers", due_minutes=30)
    active = await GoalRepository(ctx.db).list_active(limit=500)
    assert task["id"] not in {g["id"] for g in active}
    # The Work block lists it as owed (so it isn't re-registered), never as
    # a held goal the goal machinery works.
    block = await ContextAssembler(ctx).goals_block(WAITER)
    assert "Goals this conversation holds" not in block
    owed = block.split("### Owed to this conversation", 1)[1]
    assert "invisible to sweepers" in owed and "do NOT add them again" in owed
