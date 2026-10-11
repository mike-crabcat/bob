"""BridgeHealthTask state machine (2026-10-06 wedge incident).

A WhatsApp-bridge disconnect must escalate to an owner email after
ALERT_AFTER, exactly once per outage, and re-arm after recovery. The probe
and the owner alert are monkeypatched; time is driven through a patched
heartbeat.datetime so the thresholds are exercised without wall-clock
waiting."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import cast

import pytest

from server import heartbeat
from server.context import AppContext
from server.heartbeat import BridgeHealthTask


def _ctx(*, enabled: bool = True) -> AppContext:
    return cast(AppContext, SimpleNamespace(
        settings=SimpleNamespace(whatsapp_bridge=SimpleNamespace(
            enabled=enabled, url="ws://127.0.0.1:8430/ws", token="x")),
        db=None,
    ))


@pytest.fixture(autouse=True)
def _reset_state():
    BridgeHealthTask._last = None
    BridgeHealthTask._down_since = None
    BridgeHealthTask._alerted = False
    yield
    BridgeHealthTask._last = None
    BridgeHealthTask._down_since = None
    BridgeHealthTask._alerted = False


class _Clock:
    """Patches heartbeat.datetime so datetime.now(timezone.utc) is drivable."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)

    def patch(self, monkeypatch) -> None:
        cls = type("FakeDateTime", (datetime,), {"now": classmethod(
            lambda cls, tz=None: self.now)})
        monkeypatch.setattr(heartbeat, "datetime", cls)

    def advance(self, **kw) -> None:
        self.now = self.now + timedelta(**kw)


@pytest.mark.asyncio
async def test_healthy_never_alerts(monkeypatch):
    clock = _Clock()
    clock.patch(monkeypatch)
    calls: list[tuple] = []
    alerted: list[int] = []

    async def probe(self, wa):
        return True, ""
    monkeypatch.setattr(BridgeHealthTask, "_probe", probe)

    async def alert(self, ctx, down_for, reason):
        alerted.append(1)
    monkeypatch.setattr(BridgeHealthTask, "_alert_owner", alert)

    for _ in range(5):
        clock.advance(minutes=1)
        await BridgeHealthTask().run(_ctx())
    assert not alerted
    assert BridgeHealthTask._down_since is None


@pytest.mark.asyncio
async def test_outage_alerts_once_then_rearms(monkeypatch):
    clock = _Clock()
    clock.patch(monkeypatch)
    probe_results: list[tuple[bool, str]] = []
    alerted: list[timedelta] = []

    async def probe(self, wa):
        return probe_results.pop(0) if probe_results else (False, "test-down")
    monkeypatch.setattr(BridgeHealthTask, "_probe", probe)

    async def alert(self, ctx, down_for, reason):
        alerted.append(down_for)
    monkeypatch.setattr(BridgeHealthTask, "_alert_owner", alert)

    task = BridgeHealthTask()
    # First down tick: registers the outage, no alert yet.
    clock.advance(minutes=1)
    await task.run(_ctx())
    assert BridgeHealthTask._down_since is not None
    assert not alerted
    # Still inside the grace window.
    clock.advance(minutes=5)
    await task.run(_ctx())
    assert not alerted
    # Past ALERT_AFTER (15 min): one alert.
    clock.advance(minutes=10)
    await task.run(_ctx())
    assert len(alerted) == 1
    assert alerted[0] >= BridgeHealthTask._ALERT_AFTER
    # Still down many ticks later: no second alert.
    for _ in range(4):
        clock.advance(minutes=1)
        await task.run(_ctx())
    assert len(alerted) == 1
    # Recovery clears state.
    probe_results.append((True, ""))
    clock.advance(minutes=1)
    await task.run(_ctx())
    assert BridgeHealthTask._down_since is None
    # A second outage crossing the threshold alerts again.
    clock.advance(minutes=1)
    await task.run(_ctx())  # down, registers
    clock.advance(minutes=16)
    await task.run(_ctx())  # past threshold
    assert len(alerted) == 2


@pytest.mark.asyncio
async def test_kill_switch_disables(monkeypatch):
    monkeypatch.setenv("BOB_BRIDGE_HEALTH_ALERTS", "off")
    probed: list[int] = []

    async def probe(self, wa):
        probed.append(1)
        return False, "down"
    monkeypatch.setattr(BridgeHealthTask, "_probe", probe)
    await BridgeHealthTask().run(_ctx())
    assert not probed


@pytest.mark.asyncio
async def test_disabled_bridge_feature_skips(monkeypatch):
    probed: list[int] = []

    async def probe(self, wa):
        probed.append(1)
        return False, "down"
    monkeypatch.setattr(BridgeHealthTask, "_probe", probe)
    await BridgeHealthTask().run(_ctx(enabled=False))
    assert not probed


@pytest.mark.asyncio
async def test_probe_unreachable_bridge_reports_down():
    """A dead bridge process (connection refused) counts as down."""
    wa = SimpleNamespace(url="ws://127.0.0.1:1/ws")  # nothing listens on :1
    healthy, reason = await BridgeHealthTask()._probe(wa)
    assert healthy is False
    assert "unreachable" in reason
