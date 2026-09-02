"""Background process tools (bg_*).

These tests spawn real processes via systemd-run transient user units (with
a setsid fallback path), so they need a reachable user systemd manager —
skip gracefully when there isn't one. Every test cleans up its processes and
units; a tmp workspace keeps the registry isolated from the real one.

Regression context: the station-style use case needs processes that are NOT
children or cgroup members of the bob unit — `systemctl --user restart bob`
happens often and must not take background processes with it. That's why
spawning goes through `systemd-run --user` transient units rather than
plain subprocesses.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import uuid
from pathlib import Path

import pytest

from server.services import process_tools
from server.services.process_tools import make_process_tools

pytestmark = pytest.mark.skipif(
    os.environ.get("DBUS_SESSION_BUS_ADDRESS") is None
    and not Path(os.environ.get("XDG_RUNTIME_DIR", "/run/user/0")).exists(),
    reason="no user systemd manager reachable",
)


@pytest.fixture
async def bg_ctx(ctx, tmp_path):
    """AppContext whose workspace (and .bg registry) points at tmp_path."""
    ws = tmp_path / "ws"
    ws.mkdir()
    ctx.settings.harness.workspace_dir = ws
    yield ctx
    # Belt and braces: kill anything a failing test left behind.
    reg = ws / ".bg" / "processes.json"
    if reg.exists():
        for entry in json.loads(reg.read_text()).get("processes", {}).values():
            if entry.get("mechanism") == "systemd":
                await asyncio.create_subprocess_exec(
                    "systemctl", "--user", "stop", entry["unit"],
                )
            elif entry.get("pid"):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(entry["pid"], signal.SIGKILL)


def _tools(bg_ctx):
    return {t.name: t for t in make_process_tools(bg_ctx)}


async def _start(bg_ctx, name, command, **kw):
    return await _tools(bg_ctx)["bg_start"].handler(name=name, command=command, **kw)


async def _stop(bg_ctx, name):
    return await _tools(bg_ctx)["bg_stop"].handler(name=name)


def _registry(bg_ctx):
    path = bg_ctx.settings.harness.workspace_dir / ".bg" / "processes.json"
    if not path.exists():
        return {}  # blocked/failed starts never create it
    return json.loads(path.read_text())["processes"]


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
        assert "running" in status and "DEAD" not in status

        await asyncio.sleep(0.4)  # let the echo flush to the log
        logs = await _tools(bg_ctx)["bg_logs"].handler(name=name, lines=10)
        assert "bg-marker-42" in logs, logs

        pid = _registry(bg_ctx)[name]["pid"]
        stop = await _stop(bg_ctx, name)
        assert "stopped" in stop, stop
        await asyncio.sleep(0.3)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert name not in _registry(bg_ctx)
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
        pid = _registry(bg_ctx)[name]["pid"]
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


async def test_dead_entry_is_replaced_by_new_start(bg_ctx):
    name = _uniq("bgtest")
    try:
        await _start(bg_ctx, name, "sleep 60")
        pid = _registry(bg_ctx)[name]["pid"]
        os.kill(pid, signal.SIGKILL)  # die without bg_stop — entry goes stale
        await asyncio.sleep(0.3)

        status = await _tools(bg_ctx)["bg_status"].handler(name=name)
        assert "DEAD" in status, status

        res = await _start(bg_ctx, name, "sleep 60")  # same name, fresh start
        assert res.startswith(f"Started {name}"), res
        assert _registry(bg_ctx)[name]["pid"] != pid
    finally:
        await _stop(bg_ctx, name)


async def test_restart_replaces_pid(bg_ctx):
    name = _uniq("bgtest")
    try:
        await _start(bg_ctx, name, "sleep 60")
        pid1 = _registry(bg_ctx)[name]["pid"]
        res = await _tools(bg_ctx)["bg_restart"].handler(name=name)
        assert "Started" in res, res
        pid2 = _registry(bg_ctx)[name]["pid"]
        assert pid2 and pid2 != pid1
        with pytest.raises(ProcessLookupError):
            os.kill(pid1, 0)  # old one is really gone
    finally:
        await _stop(bg_ctx, name)


async def test_immediate_exit_reports_log_tail(bg_ctx):
    name = _uniq("bgtest")
    res = await _start(bg_ctx, name, "echo dying-fast; exit 3")
    assert res.startswith("Error:") and "dying-fast" in res, res
    # failed start leaves no registry entry blocking the name
    res2 = await _start(bg_ctx, name, "sleep 60")
    assert res2.startswith(f"Started {name}"), res2
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

        pid = _registry(bg_ctx)[name]["pid"]
        assert os.getsid(pid) == pid and os.getpgid(pid) == pid

        status = await _tools(bg_ctx)["bg_status"].handler(name=name)
        assert "running" in status and "DEAD" not in status

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
    assert _registry(bg_ctx) == {}


async def test_name_validation(bg_ctx):
    res = await _start(bg_ctx, "Bad Name!", "sleep 60")
    assert res.startswith("Error: invalid name"), res
    res = await _start(bg_ctx, "../escape", "sleep 60")
    assert res.startswith("Error: invalid name"), res


async def test_unknown_name_errors(bg_ctx):
    tools = _tools(bg_ctx)
    status = await tools["bg_status"].handler(name="ghost")
    assert "no process named" in status or "No background" in status
    assert "no process named" in await tools["bg_stop"].handler(name="ghost")
    assert "no process named" in await tools["bg_logs"].handler(name="ghost", lines=5)
