"""Task registry (docs/task-registry-plan.md) — the plan's invariants:

- registration creates the promise + due backstop; named completer gets
  steered (delegation)
- settle is CAS-once: duplicate completes/endpoint retries no-op with the
  settled state; delivery rides the task_settle EFFECT (idempotent)
- the waiter wake carries the D12 provenance header (who settled, when)
- completer-side visibility: tasks_block renders for the expected
  completer only; the reply turn hours later still sees the promise
- short ids resolve uniquely (chat surface, Mike's Q5)
- reconciliation: dead subagent / script-past-grace → task_fail with
  reason + waiter woken (the OOM-orphaned-Meshy class)
- task_due backstop wakes the waiter once; settle cancels it
- kill switch: BOB_TASKS=off hides tools + block
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import pytest
from unittest.mock import AsyncMock, patch

from server.repositories.tasks import TaskRepository
from server.repositories.wakeups import WakeupRepository
from server.services import tasks as task_svc

WAITER = "agent:main:whatsapp:dm:61456224867"
COMPLETER = "agent:main:whatsapp:group:120363401238199025"


async def _register(ctx, **kw):
    return await task_svc.register_task(
        ctx, waiter_session=kw.pop("waiter_session", WAITER),
        title=kw.pop("title", "meshy refine"),
        instruction=kw.pop("instruction", "poll and fetch the GLB"),
        **kw)


async def test_register_creates_promise_due_and_delegation(ctx):
    wakes = []
    with patch("server.services.wake_service.wake_conversation",
               new=AsyncMock(side_effect=lambda c, t, content, **k:
                             wakes.append((t, content)) or True)):
        task = await _register(ctx, expected_completer=COMPLETER,
                               refs=["file-bob-liebherr-v3-white"])
    assert task["status"] == "pending"
    assert task["expected_completer"] == COMPLETER

    # Due backstop scheduled against the waiter.
    due_rows = [w for w in await WakeupRepository(ctx.db).list_scheduled(WAITER)
                if w["kind"] == "task_due"]
    assert due_rows and json.loads(
        due_rows[0]["payload_json"])["task_id"] == task["id"]

    # Delegation: the completer was woken with instruction + task id.
    assert any(t == COMPLETER and task["id"] in c for t, c in wakes)

    # Default due is +1h (Mike's Q3).
    due = datetime.fromisoformat(task["due"])
    assert timedelta(0) < due - datetime.now(timezone.utc) <= timedelta(hours=1, minutes=1)


async def test_settle_cas_once_and_provenance_wake(ctx):
    task = await _register(ctx)
    with patch("server.services.wake_service.wake_conversation",
               new=AsyncMock(return_value=True)) as wake:
        out = await task_svc.settle_task(
            ctx, task["id"], to_status="completed",
            result="GLB at scratch/gnome.glb", completed_by="script:poller")
        assert out["ok"]
        # duplicate settle loses CAS
        dup = await task_svc.settle_task(
            ctx, task["id"], to_status="completed", result="again",
            completed_by="script:poller2")
        assert not dup["ok"] and dup["status"] == "completed"

        # deliver the effect twice — one wake each (idempotent replay)
        await task_svc.deliver_settlement(ctx, task["id"])
        await task_svc.deliver_settlement(ctx, task["id"])

    assert wake.await_count == 1
    content = wake.await_args.args[2]
    assert "script:poller" in content          # D12 provenance header
    assert "gnome.glb" in content
    # Due backstop cancelled on settle.
    left = [w for w in await WakeupRepository(ctx.db).list_scheduled(WAITER)
            if w["kind"] == "task_due"]
    assert not left


async def test_settle_effect_replays_after_crash(ctx):
    """The OOM class: inline delivery DIES mid-settle (wake fails), the
    effect row stays pending, replay delivers exactly once."""
    task = await _register(ctx)
    with patch("server.services.wake_service.wake_conversation",
               new=AsyncMock(side_effect=RuntimeError("oom mid-delivery"))):
        out = await task_svc.settle_task(
            ctx, task["id"], to_status="failed", error="completer died",
            completed_by="system:sweep")
    assert out["ok"]  # the settle itself landed
    row = await ctx.db.fetch_one(
        "SELECT status FROM effects WHERE kind='task_settle' "
        "AND idempotency_key = ?", (f"task_settle:{task['id']}",))
    assert row is not None          # durable effect exists
    task_row = await TaskRepository(ctx.db).get(task["id"])
    assert not task_row["delivered_at"]   # wake never landed
    # Replay (the pump's retry): delivers now, exactly once.
    with patch("server.services.wake_service.wake_conversation",
               new=AsyncMock(return_value=True)) as wake:
        await task_svc.deliver_settlement(ctx, task["id"])
        await task_svc.deliver_settlement(ctx, task["id"])
    assert wake.await_count == 1
    content = wake.await_args.args[2]
    assert "FAILED" in content and "completer died" in content


async def test_tasks_block_scoped_to_completer(ctx):
    await _register(ctx, expected_completer=COMPLETER)
    mine = await task_svc.tasks_block(COMPLETER, ctx.db)
    assert "meshy refine" in mine and "task_complete" in mine
    assert await task_svc.tasks_block(WAITER, ctx.db) == ""  # waiter sees none owed
    assert await task_svc.tasks_block(
        "agent:main:whatsapp:group:999999", ctx.db) == ""


async def test_short_id_resolution(ctx):
    task = await _register(ctx, title="short id probe")
    repo = TaskRepository(ctx.db)
    short = task["id"].removeprefix("task-")
    assert (await repo.get_by_short_id(f"task-{short}"))["id"] == task["id"]
    assert (await repo.get_by_short_id(short))["id"] == task["id"]


async def test_reconcile_fails_dead_comagent_and_stale_script(ctx):
    # dead subagent completer
    sub_id = "11111111-2222-3333-4444-555555555555"
    await ctx.db.execute(
        "INSERT INTO subagents (id, parent_session_key, session_key, task, "
        "status, created_at, updated_at) VALUES (?, 'p', 's', 't', 'failed', "
        "datetime('now'), datetime('now'))", (sub_id,))
    t1 = await _register(ctx, expected_completer=sub_id, title="sub job")
    # script completer past grace (due 3h ago → grace 1h)
    t2 = await _register(
        ctx, expected_completer="script", title="script job",
        due_at=(datetime.now(timezone.utc) - timedelta(hours=3)).isoformat())
    # healthy script (due in future) must NOT be failed
    t3 = await _register(ctx, expected_completer="script", title="fresh script")

    with patch("server.services.wake_service.wake_conversation",
               new=AsyncMock(return_value=True)):
        stats = await task_svc.reconcile_orphans(ctx)
    assert stats["failed"] == 2
    repo = TaskRepository(ctx.db)
    assert (await repo.get(t1["id"]))["status"] == "failed"
    assert "subagent failed" in (await repo.get(t1["id"]))["error"]
    assert (await repo.get(t2["id"]))["status"] == "failed"
    assert (await repo.get(t3["id"]))["status"] == "pending"


async def test_task_due_backstop_fires_once_at_waiter(ctx):
    task = await _register(ctx)
    row = [w for w in await WakeupRepository(ctx.db).list_scheduled(WAITER)
           if w["kind"] == "task_due"][0]
    await ctx.db.execute(
        "UPDATE wakeups SET not_before = datetime('now', '-1 minute') "
        "WHERE id = ?", (row["id"],))
    claimed = await WakeupRepository(ctx.db).claim_due()
    assert len(claimed) == 1
    from server.services import goal_service
    wakes = []
    with patch("server.services.wake_service.wake_conversation",
               new=AsyncMock(side_effect=lambda c, t, content, **k:
                             wakes.append((t, content)) or True)):
        reschedule = await goal_service.fire_wakeup(ctx, claimed[0])
    assert reschedule is False             # one-shot backstop
    assert wakes and wakes[0][0] == WAITER
    assert "overdue" in wakes[0][1].lower()
    # Settled task between arm and fire: no wake, series ends.
    task2 = await _register(ctx)
    await task_svc.settle_task(ctx, task2["id"], to_status="completed",
                               result="done", completed_by="test")
    row2 = {"kind": "task_due", "goal_id": None, "conversation_id": WAITER,
            "payload_json": json.dumps({"task_id": task2["id"]})}
    with patch("server.services.wake_service.wake_conversation",
               new=AsyncMock()) as wake:
        assert await goal_service.fire_wakeup(ctx, row2) is False
        wake.assert_not_awaited()



async def test_kill_switch_hides_surface(ctx, monkeypatch):
    monkeypatch.setenv("BOB_TASKS", "off")
    assert not task_svc.tasks_enabled()
    assert await task_svc.tasks_block(COMPLETER, ctx.db) == ""
    tools = {t.name: t.handler for t in task_svc.make_task_tools(ctx, WAITER)}
    res = json.loads(await tools["task_register"]("t", "i"))
    assert not res["ok"] and "disabled" in res["error"]


async def test_endpoint_token_gate_and_settle(ctx, monkeypatch):
    import server.routers.tasks_api as api

    class _Req:
        def __init__(self, token, body):
            self.headers = {"Authorization": f"Bearer {token}"}
            self._body = body

        async def json(self):
            return self._body

    monkeypatch.setenv("BOB_TASK_TOKEN", "sekrit")
    task = await _register(ctx)

    # wrong token -> 401
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        await api.settle_task_endpoint(
            task["id"], _Req("wrong", {"status": "completed"}), ctx)
    assert ei.value.status_code == 401

    # unconfigured -> 503
    monkeypatch.setenv("BOB_TASK_TOKEN", "")
    with pytest.raises(HTTPException) as ei:
        await api.settle_task_endpoint(
            task["id"], _Req("x", {"status": "completed"}), ctx)
    assert ei.value.status_code == 503

    # right token -> settled with script provenance
    monkeypatch.setenv("BOB_TASK_TOKEN", "sekrit")
    with patch("server.services.wake_service.wake_conversation",
               new=AsyncMock(return_value=True)):
        out = await api.settle_task_endpoint(
            task["id"], _Req("sekrit", {"status": "completed",
                                        "result": "GLB fetched",
                                        "by": "poller"}), ctx)
    assert out["ok"] and out["status"] == "completed"
    row = await TaskRepository(ctx.db).get(task["id"])
    assert row["completed_by"] == "script:poller"

    # retry (double-POST) -> idempotent no-op with settled state
    out2 = await api.settle_task_endpoint(
        task["id"], _Req("sekrit", {"status": "completed", "result": "again"}), ctx)
    assert not out2["ok"] and out2["status"] == "completed"
