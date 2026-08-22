"""Tests for the Bob3 core: transaction API, event log, turns, effects.

These encode plan invariants 1, 2, 4, 5, 6, 7 directly.
"""

from __future__ import annotations

import pytest

from bob_server.repositories import (
    Event,
    EventLogRepository,
    EffectRepository,
    TurnRepository,
    new_event_id,
)
from bob_server.repositories.turns import MAX_ATTEMPTS as TURN_MAX_ATTEMPTS
from bob_server.repositories.effects import MAX_ATTEMPTS as EFFECT_MAX_ATTEMPTS

CONV = "agent:main:whatsapp:dm:614000000001"


def _event(external_id: str | None = None, conversation: str = CONV, **payload) -> Event:
    return Event(
        event_type="message.received",
        binding_key=conversation,
        conversation_id=conversation,
        source="whatsapp",
        external_id=external_id,
        payload=payload or {"text": "hi"},
    )


# ------------------------------------------------------------- transactions


async def test_transaction_commits_atomically(db):
    async with db.transaction() as txn:
        await txn.execute(
            "INSERT INTO event_log (id, event_type, binding_key, conversation_id, source, occurred_at, recorded_at) "
            "VALUES ('e1', 't', 'b', 'c', 's', 'now', 'now')")
        await txn.execute(
            "INSERT INTO event_log (id, event_type, binding_key, conversation_id, source, occurred_at, recorded_at) "
            "VALUES ('e2', 't', 'b', 'c', 's', 'now', 'now')")
    rows = await db.fetch_all("SELECT id FROM event_log ORDER BY id")
    assert [r["id"] for r in rows] == ["e1", "e2"]


async def test_transaction_rolls_back_all_statements_on_error(db):
    with pytest.raises(RuntimeError, match="boom"):
        async with db.transaction() as txn:
            await txn.execute(
                "INSERT INTO event_log (id, event_type, binding_key, conversation_id, source, occurred_at, recorded_at) "
                "VALUES ('e1', 't', 'b', 'c', 's', 'now', 'now')")
            raise RuntimeError("boom")
    assert await db.fetch_all("SELECT id FROM event_log") == []


async def test_transaction_reads_see_own_writes(db):
    async with db.transaction() as txn:
        await txn.execute(
            "INSERT INTO event_log (id, event_type, binding_key, conversation_id, source, occurred_at, recorded_at) "
            "VALUES ('e1', 't', 'b', 'c', 's', 'now', 'now')")
        row = await txn.fetch_one("SELECT id FROM event_log WHERE id = 'e1'")
        assert row is not None


# ---------------------------------------------------------------- event log


async def test_append_and_get(db):
    repo = EventLogRepository(db)
    event_id = await repo.append(_event("wamid-1", text="hello"))
    assert event_id
    row = await repo.get(event_id)
    assert row["source"] == "whatsapp"
    assert row["external_id"] == "wamid-1"
    assert '"hello"' in row["payload_json"]


async def test_accept_once_per_source_external_id(db):
    """Invariant 1."""
    repo = EventLogRepository(db)
    first = await repo.append(_event("wamid-dup"))
    second = await repo.append(_event("wamid-dup"))
    assert first and second is None
    rows = await db.fetch_all("SELECT id FROM event_log")
    assert len(rows) == 1


async def test_null_external_ids_do_not_collide(db):
    repo = EventLogRepository(db)
    assert await repo.append(_event(None))
    assert await repo.append(_event(None))


async def test_append_composes_with_source_write_transactionally(db):
    """Invariant 2: source persistence + event append commit or roll back
    together."""
    repo = EventLogRepository(db)
    with pytest.raises(RuntimeError):
        async with db.transaction() as txn:
            await txn.execute(
                "INSERT INTO session_messages (session_key, role, content, created_at) "
                "VALUES (?, 'user', 'hi', datetime('now'))", (CONV,))
            await repo.append(_event("wamid-atomic"), txn=txn)
            raise RuntimeError("crash before commit")
    assert await db.fetch_all("SELECT id FROM event_log") == []
    assert await db.fetch_all("SELECT id FROM session_messages") == []


async def test_event_ids_are_time_ordered(db):
    ids = [new_event_id() for _ in range(100)]
    assert ids == sorted(ids)


# -------------------------------------------------------------------- turns


async def test_claim_takes_all_pending_events_with_watermark(db):
    """Invariants 4, 5."""
    events = EventLogRepository(db)
    turns = TurnRepository(db)
    e1 = await events.append(_event("w1"))
    e2 = await events.append(_event("w2"))

    claim = await turns.claim(CONV, lease_owner="worker-a")
    assert claim["event_ids"] == [e1, e2]
    assert claim["watermark"] == e2
    assert await turns.events_for(claim["turn_id"]) == [e1, e2]


async def test_second_claim_blocked_while_lease_active(db):
    """Invariant 4: one active turn per conversation."""
    events = EventLogRepository(db)
    turns = TurnRepository(db)
    await events.append(_event("w1"))
    assert await turns.claim(CONV, lease_owner="a")
    await events.append(_event("w2"))
    assert await turns.claim(CONV, lease_owner="b") is None


async def test_mid_turn_arrivals_stay_pending_for_next_turn(db):
    """Invariant 5: the input set is fixed at claim time."""
    events = EventLogRepository(db)
    turns = TurnRepository(db)
    await events.append(_event("w1"))
    claim = await turns.claim(CONV, lease_owner="a")
    late = await events.append(_event("w-late"))

    assert late not in await turns.events_for(claim["turn_id"])
    await turns.complete(claim["turn_id"])
    next_claim = await turns.claim(CONV, lease_owner="a")
    assert next_claim["event_ids"] == [late]


async def test_failed_turn_releases_events_for_retry(db):
    """Invariant 6."""
    events = EventLogRepository(db)
    turns = TurnRepository(db)
    await events.append(_event("w1"))
    claim = await turns.claim(CONV, lease_owner="a")
    status = await turns.fail(claim["turn_id"], "LLM exploded")
    assert status == "failed"

    retry = await turns.claim(CONV, lease_owner="a")
    assert retry is not None
    assert retry["event_ids"] == claim["event_ids"]


async def test_repeated_failures_dead_letter(db):
    """Invariant 6: attempts counted; dead-letter state exists."""
    events = EventLogRepository(db)
    turns = TurnRepository(db)
    await events.append(_event("w1"))

    last_status = None
    for _ in range(TURN_MAX_ATTEMPTS + 1):
        claim = await turns.claim(CONV, lease_owner="a")
        if claim is None:
            break
        last_status = await turns.fail(claim["turn_id"], "still broken")
    assert last_status == "dead"
    # dead turn keeps its claims: the events never re-dispatch
    assert await turns.claim(CONV, lease_owner="a") is None


async def test_expired_lease_is_reclaimed(db):
    events = EventLogRepository(db)
    turns = TurnRepository(db)
    await events.append(_event("w1"))
    stale = await turns.claim(CONV, lease_owner="a", lease_seconds=-1)
    assert stale

    fresh = await turns.claim(CONV, lease_owner="b")
    assert fresh is not None
    assert fresh["event_ids"] == stale["event_ids"]
    zombie = await turns.get(stale["turn_id"])
    assert zombie["status"] == "failed"


async def test_conversations_claim_independently(db):
    events = EventLogRepository(db)
    turns = TurnRepository(db)
    other = "agent:main:whatsapp:dm:614000000002"
    await events.append(_event("w1"))
    await events.append(_event("w2", conversation=other))

    a = await turns.claim(CONV, lease_owner="x")
    b = await turns.claim(other, lease_owner="x")
    assert a and b
    assert set(a["event_ids"]).isdisjoint(b["event_ids"])


# ------------------------------------------------------------------ effects


async def test_effect_emit_is_idempotent(db):
    """Invariant 7."""
    effects = EffectRepository(db)
    first = await effects.emit(kind="whatsapp_send", idempotency_key="k1", payload={"text": "hi"})
    second = await effects.emit(kind="whatsapp_send", idempotency_key="k1", payload={"text": "hi"})
    assert first and second is None


async def test_effect_delivery_lifecycle(db):
    effects = EffectRepository(db)
    eid = await effects.emit(kind="email_send", idempotency_key="k1", payload={})

    claimed = await effects.claim_due()
    assert [c["id"] for c in claimed] == [eid]
    assert claimed[0]["attempt"] == 1
    # claimed effects are not claimable again
    assert await effects.claim_due() == []

    await effects.mark_delivered(eid, external_result_id="msg-123")
    row = await effects.get(eid)
    assert row["status"] == "delivered"
    assert row["external_result_id"] == "msg-123"


async def test_effect_failure_backs_off_then_dead_letters(db):
    effects = EffectRepository(db)
    eid = await effects.emit(kind="call_place", idempotency_key="k1", payload={})

    (claimed,) = await effects.claim_due()
    status = await effects.mark_failed(eid, "twilio 500")
    assert status == "pending"
    row = await effects.get(eid)
    assert row["available_at"] > row["created_at"], "backoff applied"
    assert await effects.claim_due() == [], "not due until backoff elapses"

    await db.execute("UPDATE effects SET attempt = ? WHERE id = ?", (EFFECT_MAX_ATTEMPTS, eid))
    assert await effects.mark_failed(eid, "twilio 500 again") == "dead"


async def test_delivered_effect_cannot_be_redelivered_by_retry(db):
    """Invariant 7: retrying a turn cannot duplicate a delivered effect."""
    effects = EffectRepository(db)
    await effects.emit(kind="whatsapp_send", idempotency_key="turn1:reply", payload={"text": "hi"})
    (claimed,) = await effects.claim_due()
    await effects.mark_delivered(claimed["id"])

    # the retried turn re-emits with the same key: no new row, nothing due
    assert await effects.emit(kind="whatsapp_send", idempotency_key="turn1:reply", payload={"text": "hi"}) is None
    assert await effects.claim_due() == []
