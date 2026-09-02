"""Tier 2 probe context attribution (Bob3).

The probe decides partly on WHO spoke ("hey david" is a STAND_DOWN only if
David didn't send it), so the context digest must name group participants
instead of collapsing them into anonymous "User:" lines, and must keep
routine/subagent bookkeeping rows out of the transcript.
"""

from __future__ import annotations

from server.repositories.conversations import ConversationRepository
from server.repositories.participants import ParticipantRepository
from server.services.attention.tier2 import _build_context
from server.services.session_service import SessionService

SESSION = "agent:main:whatsapp:group:tier2ctx"


async def _seed(ctx) -> None:
    await ConversationRepository(ctx.db).ensure(SESSION)
    await ctx.db.execute(
        "INSERT INTO contacts (id, name, phone_number, created_at, updated_at) "
        "VALUES ('c1', 'David Shedden', '+61400000001', datetime('now'), datetime('now')), "
        "       ('c2', 'Sylvain', '+61400000002', datetime('now'), datetime('now'))")
    repo = ParticipantRepository(ctx.db)
    await repo.upsert(SESSION, "61400000001@s.whatsapp.net",
                      display_name="David Shedden", contact_id="c1",
                      now_iso="2026-08-24T00:00:00")
    await repo.upsert(SESSION, "61400000002@s.whatsapp.net",
                      display_name="Sylvain", contact_id="c2",
                      now_iso="2026-08-24T00:00:00")

    svc = SessionService(ctx)
    await svc.add_message(SESSION, "user", "anyone tried the new model?",
                          channel="whatsapp", sender_id="c1")
    await svc.add_message(SESSION, "assistant", "Yes — it's faster.",
                          channel="whatsapp")
    await svc.add_message(SESSION, "user", "hey david what did you think?",
                          channel="whatsapp", sender_id="unknown-contact")
    await svc.add_message(SESSION, "assistant", "NO_REPLY", channel="whatsapp")
    await svc.add_message(SESSION, "user", "posting the brief now",
                          channel="whatsapp", sender_id="c2", dispatched=0)
    await svc.add_message(SESSION, "user",
                          "[Routine local time: Monday 24 August 2026, 07:01 server local]\n\n"
                          "Daily rain-jacket check…",
                          channel="routine", provenance="routine")
    await svc.add_message(SESSION, "user", "[Subagent ab12cd34] Test complete.",
                          channel="subagent")


async def test_recent_conversation_names_participants(ctx):
    await _seed(ctx)
    text = await _build_context(ctx, SESSION, 10, bot_name="Bob")

    recent = text.split("## Pending")[0]
    assert "David Shedden: anyone tried the new model?" in recent
    assert "Bob: Yes — it's faster." in recent
    # Unknown sender falls back rather than crashing or misnaming.
    assert "User: hey david what did you think?" in recent
    assert "User:" != recent  # sanity: the label carries a name elsewhere


async def test_pending_batch_names_senders(ctx):
    await _seed(ctx)
    text = await _build_context(ctx, SESSION, 10, bot_name="Bob")

    pending = text.split("## Pending unprocessed messages")[1]
    assert "Sylvain: posting the brief now" in pending


async def test_routine_and_subagent_rows_excluded(ctx):
    await _seed(ctx)
    text = await _build_context(ctx, SESSION, 10, bot_name="Bob")

    assert "[Routine local time:" not in text
    assert "rain-jacket" not in text
    assert "[Subagent" not in text


async def test_stale_no_reply_rows_excluded(ctx):
    await _seed(ctx)
    text = await _build_context(ctx, SESSION, 10, bot_name="Bob")

    assert "NO_REPLY" not in text
