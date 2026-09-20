"""run_bg_process — supervised background commands with completion waking.

The script subagent type is retired (2026-09-20): run_bg_process now rides
the bg machinery (bg_jobs table + transient systemd unit + the exit
watcher in process_tools). Same advertised contract — ack, END TURN, get
woken with the output — plus no wall-clock cap and restart survival.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from server.repositories.bg_jobs import BgJobsRepository
from server.services import process_tools
from server.services.process_tools import (
    import_legacy_registry, poll_bg_jobs, start_bg_job,
)
from server.services.subagent_service import SubagentService

SESSION = "agent:main:whatsapp:dm:tester"


@pytest.fixture
def ws(ctx, monkeypatch, tmp_path):
    """Point the harness workspace at a tmp dir and fake the spawner."""
    monkeypatch.setattr(ctx.settings.harness, "workspace_dir", tmp_path)
    monkeypatch.setattr(process_tools, "_systemd_run_available", True)

    calls: list[dict] = []

    async def fake_spawn(inner_ctx, name, command, log, *, collect=True,
                         ttl_seconds=0, remain_after_exit=False):
        calls.append({"name": name, "command": command, "collect": collect,
                      "ttl_seconds": ttl_seconds,
                      "remain_after_exit": remain_after_exit})
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("fake job output\n")
        return os.getpid()

    monkeypatch.setattr(process_tools, "_spawn_systemd", fake_spawn)
    return tmp_path, calls


async def _wait_terminal(db, job_id, timeout=5.0):
    for _ in range(int(timeout / 0.05)):
        row = await db.fetch_one("SELECT * FROM bg_jobs WHERE id = ?", (job_id,))
        if row and row["status"] != "running":
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"job never went terminal: {row}")


# ------------------------------------------------------------------ retirement


@pytest.mark.asyncio
async def test_script_agent_type_retired(ctx):
    result = await SubagentService(ctx).create_subagent(
        "echo hi", SESSION, agent_type="script")
    assert result["ok"] is False
    assert "run_bg_process" in result["error"]


@pytest.mark.asyncio
async def test_untrusted_create_subagent_refused_for_every_type(ctx):
    from server.services.subagent_tools import make_subagent_tools

    tools = {t.name: t for t in make_subagent_tools(ctx, SESSION, is_trusted=False)}
    assert "run_bg_process" in tools  # the untrusted command surface stays
    for agent_type in ("claude", "local", "script", "phone-call"):
        out = json.loads(await tools["create_subagent"].handler(
            "do a thing", agent_type=agent_type))
        assert out["ok"] is False, agent_type
        assert "untrusted" in out["error"].lower()


# ------------------------------------------------------------------ start path


@pytest.mark.asyncio
async def test_start_bg_job_creates_wake_row(ctx, ws):
    tmp, calls = ws
    result = await start_bg_job(ctx, SESSION, "bash -c 'echo hi'")
    assert result["ok"] is True
    assert result["name"].startswith("job-")
    assert calls and calls[0]["collect"] is False  # jobs stay queryable
    assert calls[0]["ttl_seconds"] == 0            # trusted: uncapped
    row = await BgJobsRepository(ctx.db).get_running_by_name(result["name"])
    assert row["wake_on_exit"] == 1
    assert row["parent_session_key"] == SESSION
    assert row["source"] == "run_bg_process"


@pytest.mark.asyncio
async def test_untrusted_start_gets_ttl(ctx, ws, monkeypatch):
    tmp, calls = ws
    monkeypatch.setattr(ctx.settings.harness, "bg_untrusted_ttl_seconds", 3600)
    result = await start_bg_job(ctx, SESSION, "echo hi", is_trusted=False)
    assert result["ok"] is True
    assert calls[0]["ttl_seconds"] == 3600


@pytest.mark.asyncio
async def test_daemon_start_keeps_collect(ctx, ws):
    tmp, calls = ws
    from server.services.process_tools import make_process_tools

    tools = {t.name: t for t in make_process_tools(ctx, SESSION)}
    out = await tools["bg_start"].handler("mydaemon", "python3 server.py")
    assert out.startswith("Started mydaemon")
    assert calls[0]["collect"] is True
    row = await BgJobsRepository(ctx.db).get_running_by_name("mydaemon")
    assert row["wake_on_exit"] == 0


@pytest.mark.asyncio
async def test_sandbox_blocks_before_row(ctx, ws):
    tmp, calls = ws
    result = await start_bg_job(ctx, SESSION, "sudo rm -rf /")
    assert result["ok"] is False
    assert not calls  # never spawned
    assert await BgJobsRepository(ctx.db).count_running() == 0


# ------------------------------------------------------------------- watcher


def _state_fake(props: dict):
    async def fake(unit):
        return props
    return fake


@pytest.mark.asyncio
async def test_exit_wakes_parent_with_result(ctx, ws, monkeypatch):
    tmp, calls = ws
    result = await start_bg_job(ctx, SESSION, "bash -c 'exit 0'")
    job_id = result["job_id"]

    # RemainAfterExit parks a finished job in active(exited)
    monkeypatch.setattr(process_tools, "_unit_state", _state_fake({
        "LoadState": "loaded", "ActiveState": "active", "SubState": "exited",
        "Result": "success", "ExecMainStatus": "0",
    }))

    woken: list[tuple[str, str]] = []

    async def fake_wake(_ctx, session_key, content, **kwargs):
        woken.append((session_key, content))
        return True

    import server.services.wake_service as wake_service
    monkeypatch.setattr(wake_service, "wake_conversation", fake_wake)

    await poll_bg_jobs(ctx)

    row = await _wait_terminal(ctx.db, job_id)
    assert row["status"] == "exited"
    assert row["exit_code"] == 0
    assert row["delivery"] == "delivered"
    assert woken and woken[0][0] == SESSION
    assert "exit_code=0" in woken[0][1]
    assert "has finished" in woken[0][1]


@pytest.mark.asyncio
async def test_running_job_not_terminaled(ctx, ws, monkeypatch):
    tmp, calls = ws
    result = await start_bg_job(ctx, SESSION, "sleep 60")
    monkeypatch.setattr(process_tools, "_unit_state", _state_fake({
        "LoadState": "loaded", "ActiveState": "active", "SubState": "running",
        "Result": "success", "ExecMainStatus": "0",
    }))
    await poll_bg_jobs(ctx)
    row = await ctx.db.fetch_one("SELECT status FROM bg_jobs WHERE id = ?",
                                 (result["job_id"],))
    assert row["status"] == "running"


@pytest.mark.asyncio
async def test_failed_exit_and_wake_retry(ctx, ws, monkeypatch):
    tmp, calls = ws
    result = await start_bg_job(ctx, SESSION, "bash -c 'exit 3'")

    monkeypatch.setattr(process_tools, "_unit_state", _state_fake({
        "LoadState": "loaded", "ActiveState": "failed", "SubState": "failed",
        "Result": "exited", "ExecMainStatus": "3",
    }))

    attempts: list[str] = []

    async def flaky_wake(_ctx, session_key, content, **kwargs):
        attempts.append(session_key)
        return len(attempts) > 1  # first delivery fails (route down)

    import server.services.wake_service as wake_service
    monkeypatch.setattr(wake_service, "wake_conversation", flaky_wake)

    await poll_bg_jobs(ctx)
    row = await _wait_terminal(ctx.db, result["job_id"])
    assert row["status"] == "failed" and row["exit_code"] == 3
    assert row["delivery"] == "pending"  # owed, retried next tick

    await poll_bg_jobs(ctx)  # pending redelivery pass
    row = await ctx.db.fetch_one("SELECT delivery FROM bg_jobs WHERE id = ?",
                                 (result["job_id"],))
    assert row["delivery"] == "delivered"
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_orphaned_unit_marks_reboot(ctx, ws, monkeypatch):
    tmp, calls = ws
    result = await start_bg_job(ctx, SESSION, "echo hi")

    monkeypatch.setattr(process_tools, "_unit_state", _state_fake({
        "LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead",
        "Result": "", "ExecMainStatus": "",
    }))

    async def fake_wake(_ctx, session_key, content, **kwargs):
        return True

    import server.services.wake_service as wake_service
    monkeypatch.setattr(wake_service, "wake_conversation", fake_wake)

    await poll_bg_jobs(ctx)
    row = await _wait_terminal(ctx.db, result["job_id"])
    assert row["status"] == "orphaned"
    assert row["delivery"] == "delivered"


# ------------------------------------------------------------ exit classification


@pytest.mark.asyncio
async def test_unit_state_parses_by_key_not_position(ctx, monkeypatch):
    """systemctl show prints keyed lines in canonical order — a positional
    parse misread ExecMainStatus as Result (clean exits called failed)."""
    async def fake_run_cmd(cmd):
        return 0, "LoadState=loaded\nExecMainStatus=0\nResult=success\nActiveState=active\nSubState=exited"

    monkeypatch.setattr(process_tools, "_run_cmd", fake_run_cmd)
    props = await process_tools._unit_state("bg-x.service")
    assert props["Result"] == "success" and props["ExecMainStatus"] == "0"
    assert process_tools._classify_unit_exit(props) == ("exited", "success", 0)
    assert process_tools._job_still_running(props) is False  # parked, not running

    async def fake_run_cmd2(cmd):
        return 0, "LoadState=loaded\nResult=exited\nExecMainStatus=3"

    monkeypatch.setattr(process_tools, "_run_cmd", fake_run_cmd2)
    props = await process_tools._unit_state("bg-x.service")
    assert process_tools._classify_unit_exit(props) == ("failed", "exited", 3)

    async def fake_run_cmd3(cmd):
        return 0, "LoadState=not-found"

    monkeypatch.setattr(process_tools, "_run_cmd", fake_run_cmd3)
    props = await process_tools._unit_state("bg-x.service")
    assert process_tools._classify_unit_exit(props) == \
        ("orphaned", "unit not found (machine reboot?)", None)
    assert process_tools._job_still_running(props) is False


# ------------------------------------------------------- legacy registry import


@pytest.mark.asyncio
async def test_import_legacy_registry_alive_only(ctx, monkeypatch, tmp_path):
    monkeypatch.setattr(ctx.settings.harness, "workspace_dir", tmp_path)
    bg_dir = tmp_path / ".bg"
    bg_dir.mkdir()
    alive_pid = os.getpid()
    legacy = {"processes": {
        "radio": {"name": "radio", "unit": "bg-radio.service", "mechanism": "setsid",
                  "pid": alive_pid, "pid_start_time": None, "command": "station.py",
                  "description": "", "log": ".bg/logs/radio.log"},
        "dead-thing": {"name": "dead-thing", "mechanism": "setsid",
                       "pid": 99999999, "pid_start_time": None, "command": "x",
                       "description": "", "log": ".bg/logs/dead-thing.log"},
    }}
    (bg_dir / "processes.json").write_text(json.dumps(legacy))

    imported, skipped = await import_legacy_registry(ctx)
    assert (imported, skipped) == (1, 1)
    assert await BgJobsRepository(ctx.db).get_running_by_name("radio") is not None
    assert await BgJobsRepository(ctx.db).get_running_by_name("dead-thing") is None
    assert not (bg_dir / "processes.json").exists()
    assert (bg_dir / "processes.json.imported").exists()

    # idempotent: second run is a no-op
    assert await import_legacy_registry(ctx) == (0, 0)
