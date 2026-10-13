"""Keep-alive background daemons are restarted by the watcher after a crash
or reboot, capped per hour, with one owner alert past the cap (2026-10-13:
a 6 Oct reboot left frigate-watchd and aus-legal dead for a week)."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

from server.repositories.bg_jobs import BgJobsRepository
from server.services import process_tools
from server.services.process_tools import make_process_tools, poll_bg_jobs

GONE = {"LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead"}


@pytest.fixture
def ws(ctx, monkeypatch, tmp_path):
    monkeypatch.setattr(ctx.settings.harness, "workspace_dir", tmp_path)
    monkeypatch.setattr(process_tools, "_systemd_run_available", True)
    calls: list[dict] = []

    async def fake_spawn(inner_ctx, name, command, log, *, collect=True,
                         ttl_seconds=0, remain_after_exit=False):
        calls.append({"name": name, "command": command})
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("Traceback: boom\n")
        return os.getpid()

    monkeypatch.setattr(process_tools, "_spawn_systemd", fake_spawn)
    monkeypatch.setattr(process_tools, "_run_cmd", AsyncMock(return_value=(0, "")))
    return calls


def _tools(ctx):
    return {t.name: t for t in make_process_tools(ctx, "agent:main:whatsapp:dm:1")}


async def _start(ctx, name, **kw):
    return await _tools(ctx)["bg_start"].handler(name=name, command="python3 serve.py", **kw)


async def test_dead_keep_alive_daemon_is_restarted(ctx, ws, monkeypatch):
    assert "Keep-alive" in await _start(ctx, "svc", keep_alive=True)
    monkeypatch.setattr(process_tools, "_unit_state", AsyncMock(return_value=GONE))
    await poll_bg_jobs(ctx)                     # reboot: unit gone
    repo = BgJobsRepository(ctx.db)
    row = await repo.get_running_by_name("svc")
    assert row is not None and row["keep_alive"] == 1
    assert [c["name"] for c in ws] == ["svc", "svc"]
    assert row["command"] == "python3 serve.py"


async def test_plain_daemon_and_stopped_daemon_stay_down(ctx, ws, monkeypatch):
    await _start(ctx, "oneoff")                                   # no keep_alive
    await _start(ctx, "kept", keep_alive=True)
    await _tools(ctx)["bg_stop"].handler(name="kept")            # deliberate stop
    monkeypatch.setattr(process_tools, "_unit_state", AsyncMock(return_value=GONE))
    await poll_bg_jobs(ctx)
    repo = BgJobsRepository(ctx.db)
    assert await repo.get_running_by_name("oneoff") is None
    assert await repo.get_running_by_name("kept") is None
    assert [c["name"] for c in ws] == ["oneoff", "kept"]       # nothing respawned


async def test_crash_loop_gives_up_and_alerts_once(ctx, ws, monkeypatch):
    await _start(ctx, "flaky", keep_alive=True)
    monkeypatch.setattr(process_tools, "_unit_state", AsyncMock(return_value=GONE))
    send = AsyncMock(return_value={"ok": True})
    with patch("server.services.effects.emit_and_deliver", new=send), \
         patch("server.services.steering.owner_contact",
               new=AsyncMock(return_value={"phone_number": "+61400000001"})):
        for _ in range(5):
            await poll_bg_jobs(ctx)
    assert len(ws) == process_tools._KEEP_ALIVE_MAX_STARTS     # 1 start + 2 restarts
    assert await BgJobsRepository(ctx.db).get_running_by_name("flaky") is None
    send.assert_awaited_once()
    text = send.await_args.kwargs["payload"]["text"]
    assert "flaky keeps dying" in text and "Traceback: boom" in text


async def test_restart_preserves_keep_alive_and_wake_conflict(ctx, ws):
    await _start(ctx, "svc2", keep_alive=True)
    await _tools(ctx)["bg_restart"].handler(name="svc2")
    row = await BgJobsRepository(ctx.db).get_running_by_name("svc2")
    assert row["keep_alive"] == 1
    err = await _start(ctx, "both", keep_alive=True, wake=True)
    assert err.startswith("Error:") and "Pick one" in err
