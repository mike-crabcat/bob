"""bg engine: a dead unit never keeps its name (2026-10-09: a failed start
left bg-nightwatch-hourly-ads 'already loaded'; the job had to be renamed)."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from server.services import process_tools as pt


def _ctx(tmp_path):
    return SimpleNamespace(settings=SimpleNamespace(
        harness=SimpleNamespace(workspace_dir=tmp_path)))


@pytest.mark.asyncio
async def test_name_clash_with_dead_unit_is_cleared_and_retried(tmp_path, monkeypatch):
    calls = []
    runs = iter([(1, "Failed to start transient service unit: Unit bg-x.service "
                     "was already loaded or has a fragment file."), (0, "")])

    async def fake_run(cmd):
        calls.append(cmd)
        if cmd[0] == "systemd-run":
            return next(runs)
        if "SubState" in cmd:
            return 0, "exited\n"
        if "MainPID" in cmd:
            return 0, "4242\n"
        return 0, ""

    monkeypatch.setattr(pt, "_run_cmd", fake_run)
    monkeypatch.setattr(pt, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(pt, "_unit_env", lambda ctx: {})
    pid = await pt._spawn_systemd(_ctx(tmp_path), "x", "true", tmp_path / "x.log")
    assert pid == 4242
    flat = [" ".join(c) for c in calls]
    assert any("stop bg-x.service" in c for c in flat)
    assert any("reset-failed bg-x.service" in c for c in flat)
    assert sum(c.startswith("systemd-run") for c in flat) == 2


@pytest.mark.asyncio
async def test_running_unit_is_never_cleared(tmp_path, monkeypatch):
    calls = []

    async def fake_run(cmd):
        calls.append(cmd)
        if cmd[0] == "systemd-run":
            return 1, "Unit bg-x.service was already loaded or has a fragment file."
        if "SubState" in cmd:
            return 0, "running\n"
        return 0, ""

    monkeypatch.setattr(pt, "_run_cmd", fake_run)
    monkeypatch.setattr(pt, "_unit_env", lambda ctx: {})
    with pytest.raises(pt._SpawnError):
        await pt._spawn_systemd(_ctx(tmp_path), "x", "true", tmp_path / "x.log")
    assert not any(c[:3] == ["systemctl", "--user", "stop"] for c in calls)
