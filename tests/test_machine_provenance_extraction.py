"""Machine stimulus never triggers memory extraction (2026-09-11).

The Bob Security Guard group burned ~17% of ALL extraction tokens
digesting steer streams that concluded "Nothing to record" nearly every
time. Steers/relays/markers must not count as undigested dialogue for
the extraction trigger or the idle clock; human messages still do.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.repositories.history import HistoryRepository
from server.services.session_service import SessionService

KEY = "agent:main:whatsapp:group:test-extraction"


async def _add(svc, content, provenance=None, role="user"):
    await svc.add_message(KEY, role, content, channel="whatsapp",
                          dispatched=1, provenance=provenance)


async def test_machine_provenances_are_not_undigested(ctx, db):
    svc = SessionService(ctx)
    # a steer-only stream: nothing human to digest
    await _add(svc, "[Stimulus: frigate activity.person] person at doorbell", "steer")
    await _add(svc, "[Background task abcd1234] FINISHED — result", "steer_relay")
    await _add(svc, "[Background task abcd1234] FINISHED — result", "task_relay")
    assert await HistoryRepository(db).count_dialogue(KEY) == 0

    # human dialogue still counts
    await _add(svc, "who was at the door?")
    assert await HistoryRepository(db).count_dialogue(KEY) == 1

    # extraction markers (the extractor's own output) don't re-trigger
    await _add(svc, "Nothing to record.", "extraction_marker", role="assistant")
    assert await HistoryRepository(db).count_dialogue(KEY) == 1


async def test_machine_only_session_is_not_a_candidate(ctx, db):
    svc = SessionService(ctx)
    async def _backdate(key):
        await db.execute(
            "UPDATE messages SET created_at = datetime('now', '-5 minutes') "
            "WHERE conversation_id = ?", (key,))

    machine_key = KEY + "-machine-only"
    await svc.add_message(machine_key, "user", "[Stimulus: frigate] person",
                          channel="whatsapp", dispatched=1, provenance="steer")
    await svc.add_message(machine_key, "user", "[Background task x] FINISHED",
                          channel="whatsapp", dispatched=1, provenance="task_relay")
    await _backdate(machine_key)
    candidates = await HistoryRepository(db).extraction_candidates(
        idle_threshold_minutes=0)
    assert machine_key not in [c["session_key"] for c in candidates]

    human_key = KEY + "-human"
    await svc.add_message(human_key, "user", "who was that?",
                          channel="whatsapp", dispatched=1)
    await _backdate(human_key)
    candidates = await HistoryRepository(db).extraction_candidates(
        idle_threshold_minutes=0)
    assert human_key in [c["session_key"] for c in candidates]
