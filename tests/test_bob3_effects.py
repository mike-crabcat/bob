"""Effects outbox tests (Bob3 Phase IV).

Crash-safety proof for the outbox boundary: record-before-delivery,
idempotent duplicate suppression, retry with backoff, dead-lettering,
and pump recovery of effects stranded between record and delivery.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from server.repositories.effects import EffectRepository
from server.services import effects as effects_svc


@pytest.fixture(autouse=True)
def clean_registry():
    saved = dict(effects_svc._EXECUTORS)
    effects_svc.executors_reset_for_tests()
    yield
    effects_svc._EXECUTORS.clear()
    effects_svc._EXECUTORS.update(saved)


async def test_emit_and_deliver_happy_path(ctx, db):
    executor = AsyncMock(return_value="ext-123")
    effects_svc.register_executor("test_send", executor)

    result = await effects_svc.emit_and_deliver(
        ctx, kind="test_send", idempotency_key="k1", payload={"x": 1})

    assert result["ok"] and result["external_result_id"] == "ext-123"
    executor.assert_awaited_once()
    row = await db.fetch_one("SELECT * FROM effects WHERE idempotency_key = 'k1'")
    assert row["status"] == "delivered"
    assert row["external_result_id"] == "ext-123"
    ev = await db.fetch_one(
        "SELECT * FROM event_log WHERE event_type = 'effect.delivered'")
    assert ev is not None, "delivery observed in the event log"


async def test_duplicate_idempotency_key_suppressed(ctx, db):
    executor = AsyncMock(return_value="ext-1")
    effects_svc.register_executor("test_send", executor)

    await effects_svc.emit_and_deliver(
        ctx, kind="test_send", idempotency_key="dup", payload={})
    result2 = await effects_svc.emit_and_deliver(
        ctx, kind="test_send", idempotency_key="dup", payload={})

    assert result2["ok"] and result2.get("duplicate") is True
    assert executor.await_count == 1, "second emit must not deliver again"


async def test_retryable_failure_backs_off_then_pump_delivers(ctx, db):
    executor = AsyncMock(side_effect=[ConnectionError("bridge down"), "ext-2"])
    effects_svc.register_executor("test_send", executor)

    result = await effects_svc.emit_and_deliver(
        ctx, kind="test_send", idempotency_key="retry", payload={})
    assert not result["ok"]
    row = await db.fetch_one("SELECT * FROM effects WHERE idempotency_key = 'retry'")
    assert row["status"] == "pending", "failed delivery returns to pending with backoff"
    assert row["attempt"] == 1
    ev = await db.fetch_one("SELECT * FROM event_log WHERE event_type = 'effect.failed'")
    assert ev is not None

    # Make it due now, then pump.
    await db.execute("UPDATE effects SET available_at = '2000-01-01T00:00:00' WHERE id = ?",
                     (row["id"],))
    processed = await effects_svc.pump_due_effects(ctx)
    assert processed == 1
    row = await db.fetch_one("SELECT * FROM effects WHERE idempotency_key = 'retry'")
    assert row["status"] == "delivered"
    assert executor.await_count == 2


async def test_non_retryable_failure_goes_dead(ctx, db):
    executor = AsyncMock(side_effect=RuntimeError("twilio rejected"))
    effects_svc.register_executor("call_place", executor, retryable=False)

    result = await effects_svc.emit_and_deliver(
        ctx, kind="call_place", idempotency_key="call1", payload={})
    assert not result["ok"]
    row = await db.fetch_one("SELECT * FROM effects WHERE idempotency_key = 'call1'")
    assert row["status"] == "dead", "non-retryable kinds never re-deliver"

    processed = await effects_svc.pump_due_effects(ctx)
    assert processed == 0


async def test_crash_between_record_and_delivery_recovered_by_pump(ctx, db):
    """Simulated crash: the effect row exists (pending) but inline delivery
    never ran. The pump finds and delivers it."""
    executor = AsyncMock(return_value="ext-9")
    effects_svc.register_executor("test_send", executor)

    await EffectRepository(db).emit(
        kind="test_send", idempotency_key="orphan", payload={"x": 1})
    executor.assert_not_awaited()

    processed = await effects_svc.pump_due_effects(ctx)
    assert processed == 1
    row = await db.fetch_one("SELECT * FROM effects WHERE idempotency_key = 'orphan'")
    assert row["status"] == "delivered"


async def test_unregistered_kind_fails_safely(ctx, db):
    result = await effects_svc.emit_and_deliver(
        ctx, kind="mystery", idempotency_key="m1", payload={})
    assert not result["ok"]
    row = await db.fetch_one("SELECT * FROM effects WHERE idempotency_key = 'm1'")
    assert row["status"] in ("pending", "dead")


async def test_exhausted_attempts_dead_letter(ctx, db):
    executor = AsyncMock(side_effect=ConnectionError("always down"))
    effects_svc.register_executor("test_send", executor)

    await effects_svc.emit_and_deliver(
        ctx, kind="test_send", idempotency_key="doomed", payload={})
    for _ in range(10):
        await db.execute(
            "UPDATE effects SET available_at = '2000-01-01T00:00:00' "
            "WHERE idempotency_key = 'doomed' AND status = 'pending'")
        if await effects_svc.pump_due_effects(ctx) == 0:
            break
    row = await db.fetch_one("SELECT * FROM effects WHERE idempotency_key = 'doomed'")
    assert row["status"] == "dead"
    assert row["attempt"] >= 5


async def test_permanent_rejection_discards_without_retry(ctx, db):
    """A domain rejection (stale version, settled approval) is moot, not
    broken: no backoff attempts, no dead-letter on the ops screen — the row
    lands 'discarded' with the reason, once (2026-09-20: 8 dead rows that
    were all already-handled rejections)."""
    executor = AsyncMock(
        side_effect=effects_svc.PermanentEffectError("stale version or goal not active"))
    effects_svc.register_executor("test_goal_revise", executor)

    result = await effects_svc.emit_and_deliver(
        ctx, kind="test_goal_revise", idempotency_key="stale1", payload={})

    assert not result["ok"]
    assert result["status"] == "discarded"
    assert "stale version" in result["error"]
    row = await db.fetch_one("SELECT * FROM effects WHERE idempotency_key = 'stale1'")
    assert row["status"] == "discarded"
    assert row["attempt"] == 1, "permanent rejections must not be re-attempted"
    assert "stale version" in row["error"]

    # The pump has nothing to retry.
    await db.execute(
        "UPDATE effects SET available_at = '2000-01-01T00:00:00' "
        "WHERE idempotency_key = 'stale1'")
    assert await effects_svc.pump_due_effects(ctx) == 0
    executor.assert_awaited_once()


async def test_goal_stale_version_effect_discarded(ctx, db):
    """End-to-end wiring: update_goal against a superseded version returns
    the stale error to the caller AND discards the effect row (not dead)."""
    from server.services.goal_tools import _register_goal_executors

    _register_goal_executors()
    from server.services import goal_service
    goal = await goal_service.create_goal(
        ctx, conversation_id="test:effects:goal", objective="prove the guard")

    result = await effects_svc.emit_and_deliver(
        ctx, kind="goal_revise",
        idempotency_key=f"goal_revise:{goal['id']}:999",
        payload={"goal_id": goal["id"], "progress": "late write",
                 "expected_version": 999})

    assert not result["ok"] and result["status"] == "discarded"
    row = await db.fetch_one(
        "SELECT * FROM effects WHERE idempotency_key = ?", (f"goal_revise:{goal['id']}:999",))
    assert row["status"] == "discarded"

    # Sanity: the same executor at the CURRENT version still delivers.
    current = await db.fetch_one("SELECT version FROM goals WHERE id = ?", (goal["id"],))
    ok = await effects_svc.emit_and_deliver(
        ctx, kind="goal_revise",
        idempotency_key=f"goal_revise:{goal['id']}:{current['version']}",
        payload={"goal_id": goal["id"], "progress": "fresh write",
                 "expected_version": current["version"]})
    assert ok["ok"]
