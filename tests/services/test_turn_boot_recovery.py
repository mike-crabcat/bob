"""Restart mid-turn recovery: a deploy restart that lands mid-LLM must not
permanently consume the messages the dying turn claimed (the 2026-08-30
Dylan incident — a restart ate an in-flight question and it sat unanswered
until an unrelated nudge 10 minutes later).

Pinned: zombie turn (pending/running at boot) -> claimed messages restored
to pending, turn failed with events released so a fresh claim works.
"""

from __future__ import annotations

from bob_server.repositories import Event, EventLogRepository
from bob_server.repositories.history import HistoryRepository
from bob_server.repositories.turns import TurnRepository
from bob_server.services.session_service import SessionService

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
