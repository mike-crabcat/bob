"""SessionIdleSummaryTask must not block the heartbeat cycle: a silent
extraction waits on the session's dispatch gate, and awaiting that inline
stalled every later heartbeat task — including the wakeup pump (2026-08-29
incident: 28-min routine delay, feature set aired late).
"""

from __future__ import annotations

import asyncio

import pytest

from server.heartbeat import SessionIdleSummaryTask


@pytest.mark.asyncio
async def test_extraction_runs_detached(ctx, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    class _FakeMemoryService:
        def __init__(self, ctx):
            pass

        async def run_silent_turn_extraction(self, session_key):
            started.set()
            await release.wait()  # simulates waiting on a held session gate
            return {"status": "ok", "claims_created": 2}

    async def _fake_candidates(self, idle_threshold_minutes):
        return [{"session_key": "test:idle:1"}]

    from server.repositories.history import HistoryRepository
    from server.services import memory as memory_pkg
    monkeypatch.setattr(memory_pkg, "MemoryService", _FakeMemoryService)
    monkeypatch.setattr(HistoryRepository, "extraction_candidates",
                        _fake_candidates)

    # run() must return while the extraction is still parked on the gate
    await asyncio.wait_for(SessionIdleSummaryTask().run(ctx), timeout=1.0)
    assert await asyncio.wait_for(started.wait(), timeout=1.0), \
        "extraction was scheduled"

    release.set()
    await asyncio.sleep(0.05)  # let the detached task finish
    assert "test:idle:1" not in SessionIdleSummaryTask._pending_sessions


@pytest.mark.asyncio
async def test_one_extraction_per_session_per_tick(ctx, monkeypatch):
    """A session already parked on a gate must not stack duplicates on the
    next tick (the candidates query only marks it done once the turn runs)."""
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    class _FakeMemoryService:
        def __init__(self, ctx):
            pass

        async def run_silent_turn_extraction(self, session_key):
            calls.append(session_key)
            started.set()
            await release.wait()
            return {"status": "ok", "claims_created": 0}

    async def _fake_candidates(self, idle_threshold_minutes):
        return [{"session_key": "test:idle:2"}]

    from server.repositories.history import HistoryRepository
    from server.services import memory as memory_pkg
    monkeypatch.setattr(memory_pkg, "MemoryService", _FakeMemoryService)
    monkeypatch.setattr(HistoryRepository, "extraction_candidates",
                        _fake_candidates)

    task = SessionIdleSummaryTask()
    await task.run(ctx)
    await task.run(ctx)  # second tick while the first is gate-parked
    assert await asyncio.wait_for(started.wait(), timeout=1.0)
    assert len(calls) == 1, "no duplicate extraction stacked behind the gate"

    release.set()
    await asyncio.sleep(0.05)
