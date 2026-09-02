"""Attention coordinator tests (Bob3 Phase III cutover).

Covers Tier 1 window semantics (micro-window, group batching, sliding,
typing extension, max-wait cap), Tier 2 probe outcomes (ACT / WAIT /
STAND_DOWN / kill switch), in-flight buffering, and the recovery path.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from server.services.attention import coordinator as coord_mod
from server.services.attention.coordinator import AttentionCoordinator

SESSION = "agent:main:whatsapp:group:gtest"
DM_SESSION = "agent:main:whatsapp:dm:614"


@pytest.fixture(autouse=True)
def fast_windows(monkeypatch):
    """Shrink windows so tests run in milliseconds; reset singleton state."""
    monkeypatch.setattr(coord_mod, "WINDOW_ADDRESSED_S", 0.02)
    monkeypatch.setattr(coord_mod, "WINDOW_GROUP_S", 0.06)
    monkeypatch.setattr(coord_mod, "TYPING_EXTEND_S", 0.05)
    monkeypatch.setattr(coord_mod, "MAX_WAIT_S", 0.5)
    monkeypatch.setattr(coord_mod, "WAIT_EXTEND_S", 0.03)
    AttentionCoordinator.reset_all()
    yield
    AttentionCoordinator.reset_all()


async def _drain(seconds: float = 0.2) -> None:
    await asyncio.sleep(seconds)


async def test_dm_message_dispatches_after_micro_window(ctx):
    dispatch = AsyncMock()
    await AttentionCoordinator(ctx).submit(
        DM_SESSION, dispatch, text="hello", chat_kind="dm")
    dispatch.assert_not_awaited()  # window armed, not yet closed
    await _drain()
    dispatch.assert_awaited_once()


async def test_burst_slides_window_and_dispatches_once(ctx):
    dispatch = AsyncMock()
    c = AttentionCoordinator(ctx)
    for text in ("one", "two", "three"):
        await c.submit(DM_SESSION, dispatch, text=text, chat_kind="dm")
        await asyncio.sleep(0.005)
    await _drain()
    dispatch.assert_awaited_once()


async def test_addressed_group_message_uses_micro_window(ctx):
    dispatch = AsyncMock()
    await AttentionCoordinator(ctx).submit(
        SESSION, dispatch, text="bob, what time is it?", chat_kind="group")
    await asyncio.sleep(0.04)  # > micro, < group window
    dispatch.assert_awaited_once()


async def test_unaddressed_group_message_waits_full_window(ctx):
    dispatch = AsyncMock()
    await AttentionCoordinator(ctx).submit(
        SESSION, dispatch, text="anyone up for lunch?", chat_kind="group")
    await asyncio.sleep(0.03)  # micro-window elapsed, group window hasn't
    dispatch.assert_not_awaited()
    await _drain()
    dispatch.assert_awaited_once()  # probe disabled → ACT at close


async def test_addressed_arrival_shrinks_group_window(ctx):
    dispatch = AsyncMock()
    c = AttentionCoordinator(ctx)
    await c.submit(SESSION, dispatch, text="random chatter", chat_kind="group")
    await c.submit(SESSION, dispatch, text="hey bob help", chat_kind="group")
    await asyncio.sleep(0.04)
    dispatch.assert_awaited_once()


async def test_probe_stand_down_flushes_without_dispatch(ctx, db):
    await db.execute(
        "INSERT INTO messages (id, conversation_id, role, content, dispatched) "
        "VALUES ('m1', ?, 'user', 'blah', 0)", (SESSION,))
    dispatch = AsyncMock()
    with patch("server.services.attention.tier2.probe_actionability",
               new=AsyncMock(return_value="STAND_DOWN")):
        await AttentionCoordinator(ctx).submit(
            SESSION, dispatch, text="lol", chat_kind="group",
            probe_enabled=True)
        await _drain()
    dispatch.assert_not_awaited()
    row = await db.fetch_one("SELECT dispatched FROM messages WHERE id = 'm1'")
    assert row["dispatched"] == 1, "STAND_DOWN claims messages without the main LLM"


async def test_probe_act_dispatches(ctx):
    dispatch = AsyncMock()
    with patch("server.services.attention.tier2.probe_actionability",
               new=AsyncMock(return_value="ACT")):
        await AttentionCoordinator(ctx).submit(
            SESSION, dispatch, text="what's the capital of france",
            chat_kind="group", probe_enabled=True)
        await _drain()
    dispatch.assert_awaited_once()


async def test_probe_wait_extends_once_then_forces_act(ctx):
    dispatch = AsyncMock()
    probe = AsyncMock(return_value="WAIT")
    with patch("server.services.attention.tier2.probe_actionability", new=probe):
        await AttentionCoordinator(ctx).submit(
            SESSION, dispatch, text="hmm...", chat_kind="group",
            probe_enabled=True)
        await _drain(0.4)
    assert probe.await_count == 2, "WAIT re-probes exactly once"
    dispatch.assert_awaited_once()  # second WAIT is forced to ACT


async def test_kill_switch_bypasses_probe(ctx, monkeypatch):
    monkeypatch.setenv("BOB_ATTENTION_ALWAYS_ACT", "1")
    dispatch = AsyncMock()
    probe = AsyncMock(return_value="STAND_DOWN")
    with patch("server.services.attention.tier2.probe_actionability", new=probe):
        await AttentionCoordinator(ctx).submit(
            SESSION, dispatch, text="lol", chat_kind="group",
            probe_enabled=True)
        await _drain()
    probe.assert_not_awaited()
    dispatch.assert_awaited_once()


async def test_probe_not_consulted_for_addressed_batch(ctx):
    dispatch = AsyncMock()
    probe = AsyncMock(return_value="STAND_DOWN")
    with patch("server.services.attention.tier2.probe_actionability", new=probe):
        await AttentionCoordinator(ctx).submit(
            SESSION, dispatch, text="@bob status?", chat_kind="group",
            probe_enabled=True)
        await _drain()
    probe.assert_not_awaited()
    dispatch.assert_awaited_once()


async def test_typing_extends_window(ctx):
    dispatch = AsyncMock()
    c = AttentionCoordinator(ctx)
    await c.submit(DM_SESSION, dispatch, text="hold on", chat_kind="dm")
    await asyncio.sleep(0.01)
    c.notify_typing(DM_SESSION, "alice")   # pushes close past the micro-window
    await asyncio.sleep(0.025)              # original window would have fired
    dispatch.assert_not_awaited()
    await _drain()
    dispatch.assert_awaited_once()


async def test_max_wait_cap_bounds_typing_extensions(ctx, monkeypatch):
    monkeypatch.setattr(coord_mod, "MAX_WAIT_S", 0.05)
    dispatch = AsyncMock()
    c = AttentionCoordinator(ctx)
    await c.submit(DM_SESSION, dispatch, text="spam typing", chat_kind="dm")
    for _ in range(20):
        c.notify_typing(DM_SESSION, "alice")
        await asyncio.sleep(0.01)
    await _drain()
    dispatch.assert_awaited_once()


async def test_in_flight_dispatch_buffers_new_stimuli(ctx):
    release = asyncio.Event()
    calls = []

    async def slow_dispatch():
        calls.append(1)
        await release.wait()

    c = AttentionCoordinator(ctx)
    await c.submit(DM_SESSION, slow_dispatch, text="first", chat_kind="dm")
    await asyncio.sleep(0.05)  # window closes, dispatch starts and blocks
    assert calls == [1]
    await c.submit(DM_SESSION, slow_dispatch, text="second", chat_kind="dm")
    await asyncio.sleep(0.05)
    assert calls == [1], "no second dispatch while one is in flight"
    release.set()
    await _drain()


async def test_decisions_recorded_to_audit_table(ctx, db):
    dispatch = AsyncMock()
    c = AttentionCoordinator(ctx)
    await c.submit(DM_SESSION, dispatch, text="hi", chat_kind="dm")
    await c.submit(SESSION, dispatch, text="random chat", chat_kind="group")
    rows = await db.fetch_all(
        "SELECT session_key, addressed, decision FROM attention_shadow ORDER BY id")
    got = {(r["session_key"], r["addressed"], r["decision"]) for r in rows}
    assert (DM_SESSION, 1, "ACT") in got
    assert (SESSION, 0, "WAIT") in got
    await _drain()


async def test_resume_pending_arms_recovery_dispatch(ctx):
    dispatch = AsyncMock()
    await AttentionCoordinator(ctx).resume_pending(DM_SESSION, dispatch)
    await _drain()
    dispatch.assert_awaited_once()
