"""Message provenance: internal bookkeeping rows must not leak into
replayed LLM context, while wake nudges and routine REPLIES (genuine
conversational stimulus / group-visible output) must keep replaying.
Routine PROMPTS are internal to-do lists — routine turns build
self-contained context, so the prompts only ever leaked into OTHER turns
(2026-08-29). The `synthetic` column means "assistant reply that used
memory recall tools" and is NOT a replay filter.
"""

from __future__ import annotations

import pytest

from server.repositories.history import HistoryRepository
from server.services.session_service import SessionService


@pytest.mark.asyncio
async def test_extraction_markers_excluded_from_replay(ctx, db):
    key = "test:prov:1"
    svc = SessionService(ctx)
    await svc.add_message(key, "user", "hello")
    await svc.add_message(key, "assistant", "[Silent extraction turn: recorded 2 claim(s)]",
                          synthetic=True, provenance="extraction_marker")
    await svc.add_message(key, "assistant", "hi there!")

    rows = await HistoryRepository(db).recent_dialogue(key, limit=50)
    contents = [r["content"] for r in rows]
    assert "hi there!" in contents
    assert not any("Silent extraction" in c for c in contents)

    # Opt-in still surfaces them (ops/inspection surfaces).
    rows = await HistoryRepository(db).recent_dialogue(key, limit=50, include_internal=True)
    assert any("Silent extraction" in r["content"] for r in rows)


@pytest.mark.asyncio
async def test_dream_announcements_excluded_wake_nudges_replayed(ctx, db):
    key = "test:prov:2"
    svc = SessionService(ctx)
    await svc.add_message(key, "assistant", "psst — still keen on that plan?",
                          synthetic=True, provenance="dream_announcement")
    await svc.add_message(key, "user", "[Script finished: image ready at /tmp/car.png]",
                          provenance="wake_nudge", dispatched=0)
    await svc.add_message(key, "user", "routine prompt", provenance="routine")
    await svc.add_message(key, "assistant", "Variant B aired — promo queued",
                          provenance="routine")

    rows = await HistoryRepository(db).recent_dialogue(key, limit=50)
    contents = [r["content"] for r in rows]
    assert any("Script finished" in c for c in contents), "wake nudges must replay"
    assert any("Variant B aired" in c for c in contents), \
        "routine replies must replay (the group saw them)"
    assert not any("routine prompt" in c for c in contents), \
        "routine prompts must not replay (internal to-do lists)"
    assert not any("psst" in c for c in contents), "dream announcements must not replay"


@pytest.mark.asyncio
async def test_synthetic_alone_does_not_filter(ctx, db):
    """904 live synthetic rows are normal memory-informed replies —
    the synthetic flag must never be used as a replay filter."""
    key = "test:prov:3"
    svc = SessionService(ctx)
    await svc.add_message(key, "assistant", "Your dentist appt is Tuesday at 2pm.",
                          synthetic=True)

    rows = await HistoryRepository(db).recent_dialogue(key, limit=50)
    assert any("dentist" in r["content"] for r in rows)


@pytest.mark.asyncio
async def test_messages_default_includes_internal(ctx, db):
    """messages() serves ops/inspection surfaces — full record by default."""
    key = "test:prov:4"
    svc = SessionService(ctx)
    await svc.add_message(key, "assistant", "[Silent extraction turn: nothing memory-worthy]",
                          provenance="extraction_marker")
    rows = await HistoryRepository(db).messages(key)
    assert len(rows) == 1
    rows = await HistoryRepository(db).messages(key, include_internal=False)
    assert len(rows) == 0
