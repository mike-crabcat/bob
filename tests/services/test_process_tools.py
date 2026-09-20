"""Background process tools (bg_*).

These tests spawn real processes via systemd-run transient user units (with
a setsid fallback path), so they need a reachable user systemd manager —
skip gracefully when there isn't one. State lives in the bg_jobs table
(since 2026-09-20; replaced workspace/.bg/processes.json); every test
cleans up its processes and units, and a tmp workspace keeps logs isolated.

Regression context: the station-style use case needs processes that are NOT
children or cgroup members of the bob unit — `systemctl --user restart bob`
happens often and must not take background processes with it. That's why
spawning goes through `systemd-run --user` transient units rather than
plain subprocesses.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import uuid
from pathlib import Path

import pytest

from server.repositories.bg_jobs import BgJobsRepository
from server.services import process_tools
from server.services.process_tools import make_process_tools, poll_bg_jobs

pytestmark = pytest.mark.skipif(
    os.environ.get("DBUS_SESSION_BUS_ADDRESS") is None
    and not Path(os.environ.get("XDG_RUNTIME_DIR", "/run/user/0")).exists(),
    reason="no user systemd manager reachable",
)


@pytest.fixture
async def bg_ctx(ctx, tmp_path):
    """AppContext whose workspace (and .bg logs) points at tmp_path."""
    ws = tmp_path / "ws"
    ws.mkdir()
    ctx.settings.harness.workspace_dir = ws
    yield ctx
    # Belt and braces: kill anything a failing test left behind.
    for row in await BgJobsRepository(ctx.db).running_rows():
        if row.get("mechanism") == "systemd":
            await asyncio.create_subprocess_exec(
                "systemctl", "--user", "stop", row["unit"],
            )
            await asyncio.create_subprocess_exec(
                "systemctl", "--user", "reset-failed", row["unit"],
            )
        elif row.get("pid"):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(row["pid"], signal.SIGKILL)


def _tools(bg_ctx, session_key: str = "agent:main:test:dm"):
    return {t.name: t for t in make_process_tools(bg_ctx, session_key)}


async def _start(bg_ctx, name, command, **kw):
    return await _tools(bg_ctx)["bg_start"].handler(name=name, command=command, **kw)


async def _stop(bg_ctx, name):
    return await _tools(bg_ctx)["bg_stop"].handler(name=name)


def _row(bg_ctx, name):
    return BgJobsRepository(bg_ctx.db).get_running_by_name(name)


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------- #
# systemd-run path (primary)
# --------------------------------------------------------------------------- #

async def test_systemd_lifecycle_start_status_logs_stop(bg_ctx):
    name = _uniq("bgtest")
    try:
        res = await _start(bg_ctx, name, "echo bg-marker-$((21+21)); sleep 60")
        assert res.startswith(f"Started {name}"), res
        assert "unit bg-" in res

        status = await _tools(bg_ctx)["bg_status"].handler(name=name)
        assert "running" in status, status

        await asyncio.sleep(0.4)  # let the echo flush to the log
        logs = await _tools(bg_ctx)["bg_logs"].handler(name=name, lines=10)
        assert "bg-marker-42" in logs, logs

        pid = (await _row(bg_ctx, name))["pid"]
        stop = await _stop(bg_ctx, name)
        assert "stopped" in stop, stop
        await asyncio.sleep(0.3)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert await _row(bg_ctx, name) is None  # terminal, not running
        assert "killed" in await _tools(bg_ctx)["bg_status"].handler(
            name=name, all=True)
    finally:
        await _stop(bg_ctx, name)


def _ppid(pid: int) -> int:
    stat = Path(f"/proc/{pid}/stat").read_text()
    return int(stat[stat.rfind(")") + 1:].split()[1])


async def test_survives_independent_of_caller(bg_ctx):
    """The process must be its own session/group — not our descendant."""
    name = _uniq("bgtest")
    try:
        await _start(bg_ctx, name, "sleep 60")
        pid = (await _row(bg_ctx, name))["pid"]
        assert os.getpgid(pid) == pid, "expected a process-group leader"
        assert os.getsid(pid) == pid, "expected its own session"
        assert _ppid(pid) != os.getpid(), "must not be our child"
    finally:
        await _stop(bg_ctx, name)


async def test_duplicate_alive_name_rejected(bg_ctx):
    name = _uniq("bgtest")
    try:
        await _start(bg_ctx, name, "sleep 60")
        res = await _start(bg_ctx, name, "sleep 60")
        assert "already running" in res, res
    finally:
        await _stop(bg_ctx, name)


async def test_dead_row_is_replaced_by_new_start(bg_ctx):
    name = _uniq("bgtest")
    try:
        await _start(bg_ctx, name, "sleep 60")
        pid = (await _row(bg_ctx, name))["pid"]
        os.kill(pid, signal.SIGKILL)  # die without bg_stop — row goes stale
        await asyncio.sleep(0.3)

        await poll_bg_jobs(bg_ctx)  # watcher terminalises the dead row
        repo = BgJobsRepository(bg_ctx.db)
        row = await repo.get_running_by_name(name)
        assert row is None

        res = await _start(bg_ctx, name, "sleep 60")  # same name, fresh start
        assert res.startswith(f"Started {name}"), res
        assert (await _row(bg_ctx, name))["pid"] != pid
        rows = [r for r in await repo.list(limit=20) if r["name"] == name]
        # the watcher terminalised the old row first, so it keeps that
        # status (replaced only applies to stale running rows); history kept
        assert {r["status"] for r in rows} == {"failed", "running"}
    finally:
        await _stop(bg_ctx, name)


async def test_restart_replaces_pid(bg_ctx):
    name = _uniq("bgtest")
    try:
        await _start(bg_ctx, name, "sleep 60")
        pid1 = (await _row(bg_ctx, name))["pid"]
        res = await _tools(bg_ctx)["bg_restart"].handler(name=name)
        assert "Started" in res, res
        pid2 = (await _row(bg_ctx, name))["pid"]
        assert pid2 and pid2 != pid1
        with pytest.raises(ProcessLookupError):
            os.kill(pid1, 0)  # old one is really gone
    finally:
        await _stop(bg_ctx, name)


async def test_immediate_exit_reports_log_tail(bg_ctx):
    name = _uniq("bgtest")
    res = await _start(bg_ctx, name, "echo dying-fast; exit 3")
    assert res.startswith("Error:") and "dying-fast" in res, res
    # failed start leaves no running row blocking the name
    res2 = await _start(bg_ctx, name, "sleep 60")
    assert res2.startswith(f"Started {name}"), res2
    await _stop(bg_ctx, name)


async def test_wake_job_unit_has_no_collect_and_ttl_property(bg_ctx):
    """Jobs drop --collect (exit must stay queryable) and carry the TTL."""
    name = _uniq("bgjob")
    captured: list[list[str]] = []
    real_run_cmd = process_tools._run_cmd

    async def spy_run_cmd(cmd):
        if cmd and cmd[0] == "systemd-run":
            captured.append(cmd)
        return await real_run_cmd(cmd)

    import server.services.process_tools as pt
    orig = pt._run_cmd
    pt._run_cmd = spy_run_cmd
    try:
        res = await _start(bg_ctx, name, "sleep 60", wake=True, ttl=120)
        assert res.startswith(f"Started {name}"), res
        daemon = _uniq("bgtest")
        await _start(bg_ctx, daemon, "sleep 60")
        assert captured, "systemd-run was never called"
        job_cmd = next(c for c in captured if f"--unit=bg-{name}.service" in " ".join(c))
        daemon_cmd = next(c for c in captured if f"--unit=bg-{daemon}.service" in " ".join(c))
        assert "--collect" not in job_cmd
        assert any(a.startswith("--property=RuntimeMaxSec=120s") for a in job_cmd)
        assert "--collect" in daemon_cmd
        await _stop(bg_ctx, daemon)
    finally:
        pt._run_cmd = orig
        await _stop(bg_ctx, name)


# --------------------------------------------------------------------------- #
# setsid fallback path
# --------------------------------------------------------------------------- #

async def test_setsid_fallback_lifecycle(bg_ctx, monkeypatch):
    monkeypatch.setattr(process_tools, "_systemd_run_available", False)
    name = _uniq("bgtest")
    try:
        res = await _start(bg_ctx, name, "echo setsid-marker; sleep 60")
        assert res.startswith(f"Started {name}"), res
        assert "setsid" in res

        pid = (await _row(bg_ctx, name))["pid"]
        assert os.getsid(pid) == pid and os.getpgid(pid) == pid

        status = await _tools(bg_ctx)["bg_status"].handler(name=name)
        assert "running" in status, status

        await asyncio.sleep(0.3)
        logs = await _tools(bg_ctx)["bg_logs"].handler(name=name, lines=5)
        assert "setsid-marker" in logs, logs

        await _stop(bg_ctx, name)
        await asyncio.sleep(0.3)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        await _stop(bg_ctx, name)


# --------------------------------------------------------------------------- #
# guardrails
# --------------------------------------------------------------------------- #

async def test_sandbox_filter_blocks_unsafe_commands(bg_ctx):
    tools = _tools(bg_ctx)
    for command in ("sudo reboot", "cat ~/.ssh/id_rsa", "sqlite3 ~/data/bob.db"):
        res = await tools["bg_start"].handler(name=_uniq("bad"), command=command)
        assert res.startswith("Error:"), (command, res)
    assert await BgJobsRepository(bg_ctx.db).count_running() == 0


async def test_name_validation(bg_ctx):
    res = await _start(bg_ctx, "Bad Name!", "sleep 60")
    assert res.startswith("Error: invalid name"), res
    res = await _start(bg_ctx, "../escape", "sleep 60")
    assert res.startswith("Error: invalid name"), res


async def test_unknown_name_errors(bg_ctx):
    tools = _tools(bg_ctx)
    status = await tools["bg_status"].handler(name="ghost")
    assert "no process named" in status, status
    assert "was not running" in await tools["bg_stop"].handler(name="ghost")
    logs = await tools["bg_logs"].handler(name="ghost", lines=5)
    assert "no log at" in logs, logs  # logs are name-derived files
