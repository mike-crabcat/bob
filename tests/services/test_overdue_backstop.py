"""Overdue promise backstop (2026-10-06/07): one notice lists EVERY overdue
promise (four fired together, Bob acted on one); the series re-arms daily
up to 3 notices (a one-shot backstop let promises sit 12 days); sibling
notices in the same instant coalesce."""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import AsyncMock

from server.services import goal_service
from server.services.base import due_label, utcnow
from server.services.tasks import register_task

WAITER = "agent:main:whatsapp:group:120363000000000009"


async def _fire(ctx, task_id, notice=1):
    w = {"id": f"w-{task_id}-{notice}", "kind": "task_due", "goal_id": None,
         "conversation_id": WAITER,
         "payload_json": json.dumps({"task_id": task_id, "notice": notice})}
    return await goal_service.fire_wakeup(ctx, w)


async def test_one_notice_lists_every_overdue_promise(ctx, monkeypatch):
    goal_service._OVERDUE_LAST_WAKE.clear()
    wake = AsyncMock(return_value=True)
    monkeypatch.setattr("server.services.wake_service.wake_conversation", wake)
    monkeypatch.setattr(goal_service, "wake_conversation", wake, raising=False)
    ids = []
    for who in ("Chris", "Rupert", "Andrew"):
        t = await register_task(ctx, waiter_session=WAITER,
                                title=f"Get {who}'s roster", due_minutes=-5)
        ids.append(t["id"])
    for tid in ids:
        await _fire(ctx, tid)
    assert wake.await_count == 1, "same-instant notices coalesce"
    body = wake.await_args.args[2]
    assert all(t in body for t in ids) and "EVERY item" in body


async def test_notice_series_rearms_then_stops(ctx, monkeypatch):
    goal_service._OVERDUE_LAST_WAKE.clear()
    monkeypatch.setattr("server.services.wake_service.wake_conversation",
                        AsyncMock(return_value=True))
    t = await register_task(ctx, waiter_session=WAITER, title="x", due_minutes=-5)

    async def scheduled():
        return await ctx.db.fetch_all(
            "SELECT payload_json FROM wakeups WHERE kind='task_due' AND status='scheduled' "
            "AND json_extract(payload_json,'$.task_id') = ?", (t["id"],))
    await ctx.db.execute("UPDATE wakeups SET status='fired' WHERE kind='task_due'")
    await _fire(ctx, t["id"], notice=1)
    rows = await scheduled()
    assert len(rows) == 1 and json.loads(rows[0]["payload_json"])["notice"] == 2
    await ctx.db.execute("UPDATE wakeups SET status='fired' WHERE kind='task_due'")
    goal_service._OVERDUE_LAST_WAKE.clear()
    await _fire(ctx, t["id"], notice=3)
    assert await scheduled() == [], "series stops after the final notice"


def test_due_label_marks_overdue():
    past = (utcnow() - timedelta(hours=2)).isoformat()
    future = (utcnow() + timedelta(hours=2)).isoformat()
    assert due_label(past).startswith("OVERDUE since")
    assert due_label(future).startswith("due ")
