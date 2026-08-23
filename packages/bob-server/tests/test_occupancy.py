"""Live-session occupancy state machine tests (Bob3 Phase VI item 6)."""

from __future__ import annotations

import asyncio

import pytest

from bob_server.services import occupancy


@pytest.fixture(autouse=True)
def _reset():
    occupancy.reset_for_tests()
    yield
    occupancy.reset_for_tests()


def test_live_and_idle_lifecycle():
    assert not occupancy.is_live("conv-1")
    occupancy.mark_live("conv-1", "call-a")
    assert occupancy.is_live("conv-1")
    assert occupancy.live_count() == 1
    occupancy.mark_idle("conv-1")
    assert not occupancy.is_live("conv-1")


def test_max_occupancy_enforced():
    occupancy.mark_live("conv-1", "a")
    occupancy.mark_live("conv-2", "b")
    with pytest.raises(occupancy.OccupancyError):
        occupancy.mark_live("conv-3", "c")
    # Re-marking an already-live conversation is not a new slot.
    occupancy.mark_live("conv-1", "a2")
    occupancy.mark_idle("conv-2")
    occupancy.mark_live("conv-3", "c")  # slot freed


def test_urgent_text_detection():
    assert occupancy.is_urgent("this is URGENT please")
    assert occupancy.is_urgent("hang up the call now")
    assert occupancy.is_urgent("stop the call")
    assert not occupancy.is_urgent("what's for dinner?")
    assert not occupancy.is_urgent("")


def test_stale_live_entries_expire(monkeypatch):
    occupancy.mark_live("conv-1", "a")
    entry = occupancy._live["conv-1"]
    entry["since"] -= occupancy.LIVE_TTL_S + 1
    assert not occupancy.is_live("conv-1")


async def test_deferred_messages_drain_on_idle():
    drained: list[str] = []

    async def _drain(cid: str) -> None:
        drained.append(cid)

    occupancy.set_drain(_drain)
    occupancy.mark_live("conv-1", "call-a")
    occupancy.defer("conv-1")
    occupancy.mark_idle("conv-1")
    await asyncio.sleep(0)
    assert drained == ["conv-1"]


async def test_idle_without_deferred_does_not_drain():
    drained: list[str] = []

    async def _drain(cid: str) -> None:
        drained.append(cid)

    occupancy.set_drain(_drain)
    occupancy.mark_live("conv-1", "call-a")
    occupancy.mark_idle("conv-1")
    await asyncio.sleep(0)
    assert drained == []


async def test_mark_idle_by_ref():
    occupancy.mark_live("conv-1", "call-a")
    occupancy.mark_idle_by_ref("call-a")
    assert not occupancy.is_live("conv-1")
    occupancy.mark_idle_by_ref("unknown")  # no-op


# ------------------------------------------------- ingress integration

async def test_wa_ingress_queues_during_live_call(
        ctx, tmp_path, monkeypatch):
    """Inbound WA text during a live call is stored but not dispatched;
    urgent text bypasses the queue."""
    from tests.services.test_whatsapp_inbound_characterization import (
        _dm_payload,
        _make_service,
        _seed_contact,
        _stub_llm,
        _stub_workspace,
    )
    from tests.services.test_whatsapp_inbound_characterization import (  # noqa: F401
        immediate_patience,
        stub_memory,
    )
    # Manually apply the fixtures we need from the characterization module.
    from bob_server.services.attention import AttentionCoordinator

    _stub_workspace(monkeypatch)

    async def behaviour(messages, tools):
        return ""
    _stub_llm(monkeypatch, behaviour)

    submitted: list[str] = []
    orig_submit = AttentionCoordinator.submit

    async def _spy_submit(self, session_key, dispatch_fn, **kw):
        submitted.append(session_key)
        # Don't actually run the dispatch pipeline in this test.
        return None

    monkeypatch.setattr(AttentionCoordinator, "submit", _spy_submit)

    await _seed_contact(ctx.db, "+61400000042", trusted=1)
    key = "agent:main:whatsapp:dm:61400000042"
    svc = _make_service(ctx, tmp_path)

    occupancy.mark_live(key, "call-x")
    await svc._handle_incoming_message(_dm_payload("+61400000042", "how did it go?", "wamid-occ-1"))
    assert submitted == []  # queued, not dispatched
    assert occupancy._live[key]["deferred"] is True
    stored = await ctx.db.fetch_all(
        "SELECT dispatched FROM session_messages WHERE session_key = ? AND role='user'", (key,))
    assert stored and stored[-1]["dispatched"] == 0  # drains post-call

    await svc._handle_incoming_message(_dm_payload("+61400000042", "URGENT hang up now", "wamid-occ-2"))
    assert submitted == [key]  # urgent escape hatch dispatches immediately
