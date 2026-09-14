"""Restart mid-turn recovery: a deploy restart that lands mid-LLM must not
permanently consume the messages the dying turn claimed (the 2026-08-30
Dylan incident — a restart ate an in-flight question and it sat unanswered
until an unrelated nudge 10 minutes later).

Pinned: zombie turn (pending/running at boot) -> claimed messages restored
to pending, turn failed with events released so a fresh claim works.
"""

from __future__ import annotations

from server.repositories import Event, EventLogRepository
from server.repositories.history import HistoryRepository
from server.repositories.turns import TurnRepository
from server.services.session_service import SessionService

KEY = "agent:main:whatsapp:group:recovery-test"


async def _zombie_turn(ctx, db) -> str:
    """Store a message + ingress event, claim a turn, mark dispatched —
    the exact state a restart mid-LLM leaves behind."""
    session_svc = SessionService(ctx)
    msg_id = await session_svc.add_message(
        KEY, "user", "do we have any bob dylan tracks?", channel="whatsapp", dispatched=0)
    await EventLogRepository(db).append(Event(
        event_type="message.received", binding_key=KEY, conversation_id=KEY,
        source="whatsapp", external_id="wa-recovery-1",
        payload={"session_message_id": msg_id, "chat_kind": "group"}))
    turn = await TurnRepository(db).claim(KEY, lease_owner="dead-process")
    assert turn is not None
    await session_svc.mark_dispatched(KEY)
    return turn["turn_id"]


async def test_boot_recovery_restores_claims(ctx, db):
    turn_id = await _zombie_turn(ctx, db)

    # the message is consumed — undispatched sweep sees nothing (the old gap)
    assert await HistoryRepository(db).undispatched_conversations(channel="whatsapp") == []

    # boot sweep: restore BEFORE fail (fail releases the turn_events the
    # restore joins on)
    repo = TurnRepository(db)
    zombies = await repo.nonterminal_ids()
    assert zombies == [turn_id]
    restored = await HistoryRepository(db).restore_messages_for_turn(turn_id)
    assert restored == 1
    await repo.fail(turn_id, "process restart")

    # message pending again, conversation re-armable, turn terminal
    assert await HistoryRepository(db).undispatched_conversations(channel="whatsapp") == [KEY]
    row = await db.fetch_one("SELECT dispatched FROM messages WHERE id = "
                             "(SELECT id FROM messages WHERE conversation_id = ? "
                             " AND role = 'user' ORDER BY id DESC LIMIT 1)", (KEY,))
    assert row["dispatched"] == 0
    assert await repo.nonterminal_ids() == []

    # events released: a fresh claim (the re-armed dispatch) succeeds
    re_claim = await repo.claim(KEY, lease_owner="new-process")
    assert re_claim is not None


async def test_no_zombies_is_a_noop(ctx, db):
    repo = TurnRepository(db)
    assert await repo.nonterminal_ids() == []
    assert await HistoryRepository(db).undispatched_conversations(channel="whatsapp") == []


# ---------------------------------------------------------------------------
# Already-delivered suppression (2026-09-14 double-reply incident)
# ---------------------------------------------------------------------------

async def _seed_delivered_effect(db, dispatch_id: str) -> None:
    """A delivered whatsapp_send effect whose idempotency key embeds the
    dispatch id — the durable record that survives the process death."""
    await db.execute(
        "INSERT INTO effects (id, kind, idempotency_key, payload_json, status, "
        "attempt, available_at, delivered_at, created_at) "
        "VALUES (?, 'whatsapp_send', ?, '{}', 'delivered', 1, datetime('now'), "
        "datetime('now'), datetime('now'))",
        (f"eff-{dispatch_id[:8]}", f"whatsapp_send:{dispatch_id}:0"))


async def _seed_llm_call(db, dispatch_id: str, session_key: str) -> None:
    await db.execute(
        "INSERT INTO llm_call_log (id, provider, model, call_category, session_key, "
        "dispatch_id, status, created_at) "
        "VALUES (?, 'openrouter', 'z-ai/glm-5.3-flash', 'whatsapp_incoming', ?, ?, "
        "'running', datetime('now'))",
        (f"llm-{dispatch_id[:8]}", session_key, dispatch_id))


async def test_zombie_that_delivered_is_not_restored(ctx, db):
    """The dying turn already sent its reply (effects proof): restoring the
    claim would re-dispatch a live, answered question — the 2026-09-14
    double reply. Claims stay consumed; the turn still fails."""
    from server.main import recover_zombie_turns
    turn_id = await _zombie_turn(ctx, db)
    await _seed_llm_call(db, "070e8d85-54a2-4eef-af1d-8af72c607205", KEY)
    await _seed_delivered_effect(db, "070e8d85-54a2-4eef-af1d-8af72c607205")

    await recover_zombie_turns(db)

    # claim NOT restored — nothing to re-dispatch
    assert await HistoryRepository(db).undispatched_conversations(channel="whatsapp") == []
    row = await db.fetch_one(
        "SELECT dispatched FROM messages WHERE conversation_id = ? "
        "AND role = 'user' ORDER BY id DESC", (KEY,))
    assert row["dispatched"] == 1
    # turn is terminal either way
    assert await TurnRepository(db).nonterminal_ids() == []
    err = await db.fetch_one("SELECT error FROM turns WHERE id = ?", (turn_id,))
    assert "already delivered" in err["error"]


async def test_zombie_without_delivery_still_restores(ctx, db):
    """No delivered effect (the normal Dylan case): restore + fail as before."""
    from server.main import recover_zombie_turns
    turn_id = await _zombie_turn(ctx, db)
    # LLM call exists but nothing was delivered through the outbox.
    await _seed_llm_call(db, "8867d20c-4556-4a29-a433-b63dd447526e", KEY)

    await recover_zombie_turns(db)

    assert await HistoryRepository(db).undispatched_conversations(channel="whatsapp") == [KEY]
    assert await TurnRepository(db).nonterminal_ids() == []
    del turn_id


async def test_delivered_effect_for_other_dispatch_does_not_suppress(ctx, db):
    """Only sends from the zombie turn's own dispatch window count — a
    delivered effect belonging to a different/older dispatch must not
    suppress restoration of an unanswered question."""
    from server.main import recover_zombie_turns
    await _zombie_turn(ctx, db)
    # An unrelated dispatch delivered earlier — llm_call_log predates nothing
    # here, so give it a created_at older than the turn by using a different
    # session entirely (no llm_call_log row for KEY at all).
    await db.execute(
        "INSERT INTO llm_call_log (id, provider, model, call_category, session_key, "
        "dispatch_id, status, created_at) "
        "VALUES ('llm-other', 'openrouter', 'm', 'whatsapp_incoming', "
        "'agent:main:whatsapp:group:elsewhere', 'aaaa-bbbb-cccc', 'completed', "
        "datetime('now'))")
    await _seed_delivered_effect(db, "aaaa-bbbb-cccc")

    await recover_zombie_turns(db)

    assert await HistoryRepository(db).undispatched_conversations(channel="whatsapp") == [KEY]
