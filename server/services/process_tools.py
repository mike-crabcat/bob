"""Background process tools for LLM function calling.

The bash tool runs one command to completion — there is no way to keep a
server, watcher, or long job running between tool calls. These tools fill
that gap with a small process registry under ``<workspace>/.bg/``:

    bg_start(name, command)     start a detached, named background process
    bg_status(name?)            liveness + uptime for one or all processes
    bg_logs(name, lines)        tail a process log
    bg_stop(name)               stop it and remove the registry entry
    bg_restart(name)            stop + start the stored command

Detachment is the point: bob-server restarts frequently, so background
processes must not be children *or* cgroup members of the bob service.
``systemd-run --user`` transient units (``bg-<name>.service``) give each
process its own user unit and cgroup — it survives ``systemctl --user
restart bob`` completely, and stopping is systemd's clean TERM→KILL of the
whole tree (children included). Control does not come from parenthood: it
comes from the unit name plus this registry on disk, both of which outlive
any single bob-server instance. If systemd-run is unavailable the module
falls back to a setsid double-fork, which survives bob-server crashes but
NOT a systemd restart of the bob unit.

Commands pass the same sandbox filter as the bash tool
(``_check_command_safety``); cwd is pinned to the workspace; the skill env
(venv PATH, BOB_* secret aliases) is inherited. stdout+stderr append to
``.bg/logs/<name>.log``. Nothing here survives a machine reboot — after a
reboot the agent has to start processes again.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import re
import shlex
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from server.services.skill_env import build_skill_env
from server.services.tools import tool
from server.services.workspace_tools import _check_command_safety

if TYPE_CHECKING:
    from server.context import AppContext

logger = logging.getLogger(__name__)

_BG_DIR = ".bg"
_LOGS_SUBDIR = "logs"
_MAX_PROCESSES = 16
_MAX_LOG_CHARS = 30_000
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_UNIT_PREFIX = "bg-"
_STOP_TIMEOUT_SECONDS = 8

# Env keys forwarded into transient units. The user manager starts units
# with a clean environment, so without these the venv python, workspace
# pointer, and skill secrets (OPENAI_API_KEY etc.) would be missing.
_FORWARDED_ENV_KEYS = (
    "PATH", "VIRTUAL_ENV", "HOME", "LANG", "BOB_WORKSPACE_DIR",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "AGENTMAIL_API_KEY",
    "GOOGLE_PLACES_API_KEY", "GIPHY_API_KEY",
)

_systemd_run_available = bool(shutil.which("systemd-run"))


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #

def _bg_root(ctx: AppContext) -> Path:
    return ctx.settings.harness.workspace_dir.expanduser().resolve() / _BG_DIR


def _registry_path(ctx: AppContext) -> Path:
    return _bg_root(ctx) / "processes.json"


def _log_path(ctx: AppContext, name: str) -> Path:
    return _bg_root(ctx) / _LOGS_SUBDIR / f"{name}.log"


def _load_registry(ctx: AppContext) -> dict[str, dict]:
    path = _registry_path(ctx)
    try:
        data = json.loads(path.read_text())
        return data.get("processes", {})
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_registry(ctx: AppContext, processes: dict[str, dict]) -> None:
    root = _bg_root(ctx)
    root.mkdir(parents=True, exist_ok=True)
    (root / _LOGS_SUBDIR).mkdir(parents=True, exist_ok=True)
    tmp = root / "processes.json.tmp"
    tmp.write_text(json.dumps({"processes": processes}, indent=2))
    os.replace(tmp, _registry_path(ctx))


# --------------------------------------------------------------------------- #
# liveness
# --------------------------------------------------------------------------- #

def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user


def _pid_start_time(pid: int) -> str | None:
    """Field 22 of /proc/<pid>/stat — guards against pid reuse."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        rest = stat[stat.rfind(")") + 1:].split()
        return rest[19] if len(rest) > 19 else None
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return None


async def _run_cmd(cmd: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    return proc.returncode or 0, out.decode("utf-8", errors="replace").strip()


async def _entry_alive(entry: dict) -> bool:
    if entry.get("mechanism") == "systemd":
        rc, _ = await _run_cmd([
            "systemctl", "--user", "is-active", "--quiet",
            f"{_UNIT_PREFIX}{entry['name']}.service",
        ])
        return rc == 0
    return _pid_alive(entry.get("pid")) and (
        entry.get("pid_start_time") is None
        or _pid_start_time(entry["pid"]) == entry.get("pid_start_time")
    )


def _tail_file(path: Path, lines: int) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            data = f.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return f"Error: no log at {path} (process never started?)"
    tail = data.splitlines()[-lines:]
    body = "\n".join(tail)
    if len(body) > _MAX_LOG_CHARS:
        body = body[-_MAX_LOG_CHARS:]
    return body


# --------------------------------------------------------------------------- #
# spawn / terminate
# --------------------------------------------------------------------------- #

class _SpawnError(Exception):
    pass


def _unit_env(ctx: AppContext) -> dict[str, str]:
    settings = ctx.settings
    ws = settings.harness.workspace_dir.expanduser().resolve()
    env = build_skill_env(
        workspace_dir=str(ws),
        venv_dir=str(settings.harness.venv_dir.expanduser()),
    )
    return {k: env[k] for k in _FORWARDED_ENV_KEYS if env.get(k)}


async def _spawn_systemd(ctx: AppContext, name: str, command: str, log: Path) -> int:
    """Start as a transient user unit. Returns the MainPID."""
    ws = ctx.settings.harness.workspace_dir.expanduser().resolve()
    unit = f"{_UNIT_PREFIX}{name}.service"
    inner = f"exec bash -c {shlex.quote(command)} >> {shlex.quote(str(log))} 2>&1"
    cmd = [
        "systemd-run", "--user", "--collect", f"--unit={unit}",
        f"--working-directory={ws}",
        f"--property=TimeoutStopSec={_STOP_TIMEOUT_SECONDS}",
    ]
    for key, value in _unit_env(ctx).items():
        cmd.append(f"--setenv={key}={value}")
    cmd += ["bash", "-c", inner]

    rc, out = await _run_cmd(cmd)
    if rc != 0 and "already exists" in out:
        # A just-died unit may not be garbage-collected yet (--collect is
        # prompt but not instant). Clear the failed state and retry once.
        await _run_cmd(["systemctl", "--user", "reset-failed", unit])
        rc, out = await _run_cmd(cmd)
    if rc != 0:
        if "already exists" in out:
            raise _SpawnError(
                f"unit {unit} already exists and refuses to clear — "
                f"systemctl --user stop {unit} then retry"
            )
        raise _SpawnError(f"systemd-run failed: {out or 'unknown error'}")

    await asyncio.sleep(0.3)  # let the unit activate and exec
    rc, pid_out = await _run_cmd([
        "systemctl", "--user", "show", unit, "-p", "MainPID", "--value",
    ])
    pid = int(pid_out.strip() or 0)

    rc_active, _ = await _run_cmd([
        "systemctl", "--user", "is-active", "--quiet", unit,
    ])
    if rc_active != 0 or not _pid_alive(pid):
        raise _SpawnError(
            f"process exited immediately. Log tail:\n{_tail_file(log, 15)}"
        )
    return pid


async def _spawn_setsid(ctx: AppContext, name: str, command: str, log: Path) -> int:
    """Fallback when systemd-run is unavailable: setsid double-fork.

    Survives bob-server crashes but not a systemd restart of the bob unit
    (the process stays in bob's cgroup).
    """
    ws = ctx.settings.harness.workspace_dir.expanduser().resolve()
    root = _bg_root(ctx)
    launcher = root / f"{name}.sh"
    pidfile = root / f"{name}.pid"
    pidfile.unlink(missing_ok=True)
    body = (
        "setsid bash -c "
        f"{shlex.quote(command)} >> {shlex.quote(str(log))} 2>&1 < /dev/null &\n"
        f"echo $! > {shlex.quote(str(pidfile))}\n"
    )
    launcher.write_text(body)

    proc = await asyncio.create_subprocess_exec(
        "bash", str(launcher),
        cwd=str(ws),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=build_skill_env(
            workspace_dir=str(ws),
            venv_dir=str(ctx.settings.harness.venv_dir.expanduser()),
        ),
    )
    await asyncio.wait_for(proc.wait(), timeout=10)
    await asyncio.sleep(0.2)

    try:
        pid = int(pidfile.read_text().strip())
    except (FileNotFoundError, ValueError):
        raise _SpawnError(
            f"spawn failed (no pidfile). Log tail:\n{_tail_file(log, 15)}"
        )
    if not _pid_alive(pid):
        raise _SpawnError(
            f"process exited immediately. Log tail:\n{_tail_file(log, 15)}"
        )
    return pid


async def _terminate(entry: dict) -> str:
    name = entry["name"]
    if entry.get("mechanism") == "systemd":
        rc, out = await _run_cmd([
            "systemctl", "--user", "stop",
            f"{_UNIT_PREFIX}{name}.service",
        ])
        return "stopped" if rc == 0 else f"systemctl stop failed: {out}"

    pid = entry.get("pid")
    if not pid or not _pid_alive(pid):
        return "was not running"
    try:
        os.killpg(pid, 15)  # setsid made it the group leader
    except (ProcessLookupError, PermissionError):
        os.kill(pid, 15)
    for _ in range(25):
        if not _pid_alive(pid):
            return "stopped"
        await asyncio.sleep(0.2)
    try:
        os.killpg(pid, 9)
    except (ProcessLookupError, PermissionError):
        pass
    return "killed (did not exit on SIGTERM)"


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #

def make_process_tools(ctx: AppContext):
    """Create the bg_* background-process tools bound to the given context."""

    @tool
    async def bg_start(
        name: str,
        command: str,
        description: str = "",
    ) -> str:
        """Start a long-running background process (server, watcher, daemon)
        that keeps running across tool calls and bob-server restarts. Give it
        a stable lowercase name — the same name stops/inspects it later. The
        command runs via bash with the workspace as cwd and the same sandbox
        rules as the bash tool; output appends to a log readable with bg_logs.
        Returns an error if the process exits immediately — check bg_logs.
        Example: bg_start(name="bob-fm", command="python3 skills/radio/station.py")
        """
        if not _NAME_RE.fullmatch(name):
            return (
                f"Error: invalid name {name!r} — use lowercase letters, digits, "
                "dot, dash, underscore (max 64 chars)"
            )
        violation = _check_command_safety(
            command,
            db_path=ctx.settings.db_path,
            data_dir=ctx.settings.data_dir,
            config_dir=ctx.settings.config_dir,
        )
        if violation:
            logger.warning("bg_start blocked by sandbox: %r — %s", command, violation)
            return f"Error: {violation}"

        processes = _load_registry(ctx)
        if name in processes and await _entry_alive(processes[name]):
            return (
                f"Error: {name} is already running "
                f"(pid {processes[name].get('pid')}). Use bg_stop {name} first, "
                f"or bg_restart {name} to replace it."
            )
        alive = [n for n, e in processes.items() if await _entry_alive(e)]
        if len(alive) >= _MAX_PROCESSES:
            return (
                f"Error: {len(alive)} background processes already running "
                f"(max {_MAX_PROCESSES}): {', '.join(sorted(alive))}. "
                "Stop something first."
            )

        log = _log_path(ctx, name)
        log.parent.mkdir(parents=True, exist_ok=True)
        logger.info("bg_start: %s: %s", name, command)

        mechanism, pid = "setsid", None
        try:
            if _systemd_run_available:
                mechanism = "systemd"
                pid = await _spawn_systemd(ctx, name, command, log)
            else:
                pid = await _spawn_setsid(ctx, name, command, log)
        except _SpawnError as exc:
            return f"Error: {exc}"

        entry = {
            "name": name,
            "unit": f"{_UNIT_PREFIX}{name}.service" if mechanism == "systemd" else None,
            "mechanism": mechanism,
            "pid": pid,
            "pid_start_time": _pid_start_time(pid),
            "command": command,
            "description": description,
            "started_at": _dt.datetime.now().astimezone().isoformat(),
            "log": str(log.relative_to(ctx.settings.harness.workspace_dir.expanduser().resolve())),
        }
        processes[name] = entry
        _save_registry(ctx, processes)
        how = f"unit {entry['unit']}" if mechanism == "systemd" else "setsid"
        return f"Started {name} ({how}, pid {pid}). bg_status to check, bg_logs {name} for output."

    @tool
    async def bg_status(name: str = "") -> str:
        """Check background processes started with bg_start. With no name,
        lists every registered process with pid, uptime, and liveness; with a
        name, reports just that one. Dead entries stay listed (with their log
        path) until replaced by a new bg_start or removed by bg_stop."""
        processes = _load_registry(ctx)
        if not processes:
            return "No background processes registered."

        names = [name] if name else sorted(processes)
        if name and name not in processes:
            return f"Error: no process named {name!r}. bg_status with no args lists all."

        lines = []
        running = 0
        for n in names:
            e = processes[n]
            alive = await _entry_alive(e)
            running += int(alive)
            started = _dt.datetime.fromisoformat(e["started_at"])
            uptime = _dt.datetime.now().astimezone() - started
            state = "running" if alive else "DEAD"
            desc = f" — {e['description']}" if e.get("description") else ""
            lines.append(
                f"{n}: {state} (pid {e.get('pid')}, up {str(uptime).split('.')[0]}"
                f"{', ' + e['unit'] if e.get('unit') else ''}){desc}\n"
                f"  command: {e['command']}\n  log: {e['log']}"
            )
        return f"{running}/{len(names)} running\n\n" + "\n\n".join(lines)

    @tool
    async def bg_logs(name: str, lines: int = 50) -> str:
        """Read the last N lines (default 50) of a background process's log.
        Use this to check on a process after bg_start or when it misbehaves —
        stdout and stderr both land in the log."""
        if lines < 1 or lines > 500:
            return "Error: lines must be between 1 and 500."
        processes = _load_registry(ctx)
        if name not in processes:
            return f"Error: no process named {name!r}. bg_status lists registered names."
        ws = ctx.settings.harness.workspace_dir.expanduser().resolve()
        path = ws / processes[name]["log"]
        return _tail_file(path, lines) or "(log is empty)"

    @tool
    async def bg_stop(name: str) -> str:
        """Stop a background process started with bg_start and remove its
        registry entry. The whole process tree is terminated cleanly (TERM,
        then KILL after a grace period). Stopping an already-dead process just
        cleans up its registry entry."""
        processes = _load_registry(ctx)
        if name not in processes:
            return f"Error: no process named {name!r}. bg_status lists registered names."
        entry = processes.pop(name)
        outcome = await _terminate(entry)
        _save_registry(ctx, processes)
        logger.info("bg_stop: %s — %s", name, outcome)
        return f"{name}: {outcome}. Registry entry removed."

    @tool
    async def bg_restart(name: str) -> str:
        """Restart a background process using its stored command: stops the
        running instance (if any) and starts a fresh one under the same name."""
        processes = _load_registry(ctx)
        if name not in processes:
            return f"Error: no process named {name!r}. bg_status lists registered names."
        entry = processes.pop(name)
        outcome = await _terminate(entry)
        _save_registry(ctx, processes)
        # Re-run the start path with the stored command (bg_start is a Tool
        # instance here, so call through .handler).
        result = await bg_start.handler(
            name, entry["command"], entry.get("description", ""),
        )
        return f"stopped ({outcome}); {result}"

    return [bg_start, bg_status, bg_logs, bg_stop, bg_restart]
