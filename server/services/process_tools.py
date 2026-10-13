"""Background process tools for LLM function calling.

The bash tool runs one command to completion — there is no way to keep a
server, watcher, or long job running between tool calls. These tools fill
that gap, backed by the ``bg_jobs`` table (the DB registry; replaced the
old ``workspace/.bg/processes.json`` 2026-09-20):

    bg_start(name, command, ...) start a detached, named background process
    bg_status(name?, all)        liveness + uptime for one or all processes
    bg_logs(name, lines)         tail a process log
    bg_stop(name)                stop it and mark the row killed
    bg_restart(name)             stop + start the stored command

Two kinds of entries share the machinery:

    daemons (bg_start default) — servers/watchers; no exit notification;
        dead rows just go terminal in the table.
    jobs (wake=True / run_bg_process) — one-shot commands whose completion
        WAKES the starting conversation: the exit watcher loop below polls
        systemd, records the exit, and delivers the wake until it lands.
        Job units drop --collect so the exited unit stays queryable until
        the watcher has read Result/ExecMainStatus and reset-failed it.

Detachment is the point: bob-server restarts frequently, so background
processes must not be children *or* cgroup members of the bob service.
``systemd-run --user`` transient units (``bg-<name>.service``) give each
process its own user unit and cgroup — it survives ``systemctl --user
restart bob`` completely, and stopping is systemd's clean TERM→KILL of
the whole tree (children included). Control does not come from parenthood:
it comes from the unit name plus the bg_jobs rows, both of which outlive
any single bob-server instance. If systemd-run is unavailable the module
falls back to a setsid double-fork, which survives bob-server crashes but
NOT a systemd restart of the bob unit.

Commands pass the same sandbox filter as the bash tool
(``_check_command_safety``); cwd is pinned to the workspace; the skill env
(venv PATH, BOB_* secret aliases) is inherited. stdout+stderr append to
``.bg/logs/<name>.log``. Nothing survives a machine reboot — after a
reboot the agent has to start processes again (the watcher marks rebooted
rows 'orphaned' and still delivers owed wakes).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import re
import secrets
import shlex
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from server.repositories.bg_jobs import BgJobsRepository
from server.services.base import utcnow
from server.services.skill_env import build_skill_env
from server.services.tools import tool
from server.services.workspace_tools import _check_command_safety

if TYPE_CHECKING:
    from server.context import AppContext

logger = logging.getLogger(__name__)

_BG_DIR = ".bg"
_LOGS_SUBDIR = "logs"
_MAX_PROCESSES = 24
# keep-alive daemons: starts per name per rolling hour before the watcher
# stops restarting and alerts the owner (a crash loop, not a blip).
_KEEP_ALIVE_MAX_STARTS = 3
_MAX_LOG_CHARS = 30_000
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_UNIT_PREFIX = "bg-"
_STOP_TIMEOUT_SECONDS = 8
_WATCH_INTERVAL_SECONDS = 5.0
_FORWARDED_ENV_KEYS = (
    "PATH", "VIRTUAL_ENV", "HOME", "LANG", "BOB_WORKSPACE_DIR",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "AGENTMAIL_API_KEY",
    "GOOGLE_PLACES_API_KEY", "GIPHY_API_KEY",
)
_systemd_run_available = bool(shutil.which("systemd-run"))


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #

def _bg_root(ctx: AppContext) -> Path:
    return ctx.settings.harness.workspace_dir.expanduser().resolve() / _BG_DIR


def _log_path(ctx: AppContext, name: str) -> Path:
    return _bg_root(ctx) / _LOGS_SUBDIR / f"{name}.log"


def _legacy_registry_path(ctx: AppContext) -> Path:
    return _bg_root(ctx) / "processes.json"


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
        return stat.rsplit(")", 1)[1].split()[19]
    except (FileNotFoundError, IndexError, ProcessLookupError):
        return None


async def _entry_alive(entry: dict) -> bool:
    """Works on bg_jobs rows and legacy entries (same keys)."""
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

async def _run_cmd(cmd: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    return proc.returncode or 0, out.decode("utf-8", errors="replace").strip()


class _SpawnError(Exception):
    pass


def _unit_env(ctx: AppContext) -> dict[str, str]:
    env = build_skill_env(
        workspace_dir=str(ctx.settings.harness.workspace_dir.expanduser().resolve()),
        venv_dir=str(ctx.settings.harness.venv_dir.expanduser().resolve()),
    )
    return {k: v for k, v in env.items() if k in _FORWARDED_ENV_KEYS}


async def _spawn_systemd(
    ctx: AppContext, name: str, command: str, log: Path, *,
    collect: bool = True, ttl_seconds: int = 0, remain_after_exit: bool = False,
) -> int:
    """Spawn via a transient user unit; returns the MainPID.

    Wake jobs set remain_after_exit: systemd garbage-collects successfully-
    exited transient units IMMEDIATELY (--collect only covers failed ones),
    which races the watcher's Result/ExecMainStatus read. RemainAfterExit
    parks the finished unit in active(exited) — properties readable for as
    long as needed; the watcher stops + reset-fails it once recorded.
    ttl_seconds adds RuntimeMaxSec: systemd kills the job at the cap
    (Result=timeout).
    """
    unit = f"{_UNIT_PREFIX}{name}.service"
    inner = f"exec bash -c {shlex.quote(command)} >> {shlex.quote(str(log))} 2>&1"
    cmd = [
        "systemd-run", "--user",
        f"--unit={unit}",
        f"--working-directory={ctx.settings.harness.workspace_dir.expanduser().resolve()}",
        f"--property=TimeoutStopSec={_STOP_TIMEOUT_SECONDS}",
    ]
    if collect:
        cmd.append("--collect")
    if remain_after_exit:
        cmd.append("--property=RemainAfterExit=yes")
    if ttl_seconds > 0:
        cmd.append(f"--property=RuntimeMaxSec={int(ttl_seconds)}s")
    for key, value in _unit_env(ctx).items():
        cmd.append(f"--setenv={key}={value}")
    cmd += ["bash", "-c", inner]

    rc, out = await _run_cmd(cmd)
    if rc != 0 and _unit_name_taken(out):
        # A just-died unit may not be garbage-collected yet (--collect is
        # prompt but not instant), or a RemainAfterExit unit is parked in
        # active(exited) after an immediate exit (2026-10-09: a failed
        # start left bg-nightwatch-hourly-ads "already loaded" and the name
        # unusable). Clear it — only if nothing is actually running.
        if await _clear_dead_unit(unit):
            rc, out = await _run_cmd(cmd)
    if rc != 0:
        if _unit_name_taken(out):
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
        await _clear_dead_unit(unit)  # don't leave the name occupied
        raise _SpawnError(
            f"process exited immediately. Log tail:\n{_tail_file(log, 15)}"
        )
    return pid


def _unit_name_taken(out: str) -> bool:
    return any(s in (out or "") for s in (
        "already exists", "already loaded", "has a fragment file"))


async def _clear_dead_unit(unit: str) -> bool:
    """Stop + reset-failed a unit whose process is gone (SubState exited /
    failed / dead). Returns False — and touches nothing — while it runs."""
    _, sub_state = await _run_cmd(
        ["systemctl", "--user", "show", unit, "-p", "SubState", "--value"])
    if sub_state.strip() in ("running", "start", "start-pre", "start-post",
                             "reload", "auto-restart"):
        return False
    await _run_cmd(["systemctl", "--user", "stop", unit])
    await _run_cmd(["systemctl", "--user", "reset-failed", unit])
    return True


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
# start core (shared by bg_start and run_bg_process)
# --------------------------------------------------------------------------- #

async def _start_core(
    ctx: AppContext, *, name: str, command: str, description: str,
    wake: bool, ttl_seconds: int, parent_session_key: str, source: str,
    keep_alive: bool = False,
) -> tuple[int | None, str]:
    """Validate + spawn + insert the bg_jobs row. Returns (row_id, message);
    row_id is None on failure with the message explaining why."""
    if not _NAME_RE.fullmatch(name):
        return None, (
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
        logger.warning("bg start blocked by sandbox: %r — %s", command, violation)
        return None, f"Error: {violation}"
    if keep_alive and wake:
        return None, ("Error: keep_alive is for daemons that should stay up; "
                      "wake=True is for one-shot jobs that finish. Pick one.")
    if wake and not parent_session_key:
        return None, "Error: wake=True needs a session to wake (no session binding here)."

    # Quoting preflight (ported from the retired script executor, 2026-09-05
    # crayon-portrait goal): a command the shell can't even parse used to
    # surface only as a failed background run minutes later. Reject up front.
    from server.services.workspace_tools import bash_syntax_check
    syntax_error = await bash_syntax_check(command)
    if syntax_error:
        return None, (
            f"Error: command blocked by shell syntax: {syntax_error} — these "
            "tools take a shell COMMAND, not a prose brief (briefs belong "
            "to create_subagent agent_type='claude'). Fix the quoting: "
            "backslash does not escape apostrophes in single quotes; use a "
            "heredoc or --prompt-file for prose"
        )

    repo = BgJobsRepository(ctx.db)
    existing = await repo.get_running_by_name(name)
    if existing is not None and await _entry_alive(existing):
        return None, (
            f"Error: {name} is already running (pid {existing.get('pid')}). "
            f"Use bg_stop {name} first, or bg_restart {name} to replace it."
        )
    if await repo.count_running() >= _MAX_PROCESSES:
        alive = ", ".join(sorted(r["name"] for r in await repo.running_rows()))
        return None, (
            f"Error: {_MAX_PROCESSES} background processes already running "
            f"(max {_MAX_PROCESSES}): {alive}. Stop something first."
        )

    log = _log_path(ctx, name)
    log.parent.mkdir(parents=True, exist_ok=True)
    logger.info("bg start (%s): %s: %s", source, name, command)

    mechanism, pid = "setsid", None
    try:
        if _systemd_run_available:
            mechanism = "systemd"
            pid = await _spawn_systemd(
                ctx, name, command, log,
                collect=not wake, ttl_seconds=ttl_seconds, remain_after_exit=wake,
            )
        else:
            pid = await _spawn_setsid(ctx, name, command, log)
    except _SpawnError as exc:
        return None, f"Error: {exc}"

    if existing is not None:  # dead row under this name — keep as history
        await repo.mark_replaced(existing["id"], utcnow().isoformat())

    ws = ctx.settings.harness.workspace_dir.expanduser().resolve()
    job_id = await repo.create(
        name=name,
        command=command,
        unit=f"{_UNIT_PREFIX}{name}.service" if mechanism == "systemd" else None,
        mechanism=mechanism,
        pid=pid,
        pid_start_time=_pid_start_time(pid),
        description=description,
        source=source,
        wake_on_exit=wake,
        parent_session_key=parent_session_key,
        log=str(log.relative_to(ws)),
        now_iso=utcnow().isoformat(),
        keep_alive=keep_alive,
    )
    how = f"unit bg-{name}.service" if mechanism == "systemd" else "setsid"
    tail = (
        " You'll be woken with the output when it finishes; send any artifact then."
        if wake else " bg_status to check, bg_logs for output."
    )
    if keep_alive:
        tail += " Keep-alive: restarted automatically if it dies or the machine reboots."
    return job_id, f"Started {name} ({how}, pid {pid}).{tail}"


async def start_bg_job(
    ctx: AppContext, session_key: str, command: str, *, is_trusted: bool = True,
    name: str = "", description: str = "",
) -> dict:
    """The engine behind run_bg_process: a uniquely-named wake job with no
    wall-clock cap for trusted sessions (mandatory RuntimeMaxSec TTL for
    untrusted ones). An explicit name replaces the random job-<hex> handle —
    models kept passing name=/description= to the tool (13 concflation
    TypeErrors in the week to 2026-09-22), so the tool accepts them."""
    ttl = 0 if is_trusted else int(ctx.settings.harness.bg_untrusted_ttl_seconds)
    name = name.strip() or f"job-{secrets.token_hex(4)}"
    job_id, message = await _start_core(
        ctx, name=name, command=command, description=description,
        wake=True, ttl_seconds=ttl, parent_session_key=session_key,
        source="run_bg_process",
    )
    if job_id is None:
        return {"ok": False, "error": message.removeprefix("Error: ")}
    return {"ok": True, "name": name, "job_id": job_id,
            "log": f".bg/logs/{name}.log", "detail": message}


# --------------------------------------------------------------------------- #
# exit watcher
# --------------------------------------------------------------------------- #

async def _unit_state(unit: str) -> dict[str, str]:
    """Keyed `systemctl show` read. systemctl prints properties in ITS
    canonical order, not request order — parse the Prop=value lines by key,
    never by position (the positional version read ExecMainStatus as Result
    and called clean exits failed)."""
    rc, out = await _run_cmd([
        "systemctl", "--user", "show", unit,
        "-p", "LoadState", "-p", "ActiveState", "-p", "SubState",
        "-p", "Result", "-p", "ExecMainStatus",
    ])
    props: dict[str, str] = {}
    for line in (out or "").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            props[key.strip()] = value.strip()
    return props


def _classify_unit_exit(props: dict[str, str]) -> tuple[str, str, int | None]:
    """(status, systemd_result, exit_code) for a finished job unit."""
    if props.get("LoadState") == "not-found":
        return "orphaned", "unit not found (machine reboot?)", None
    result = props.get("Result", "")
    try:
        exit_code: int | None = int(props.get("ExecMainStatus", ""))
    except ValueError:
        exit_code = None
    if result == "success":
        return "exited", result, exit_code
    if result == "timeout":
        return "timeout", result, exit_code
    return "failed", result or "unknown", exit_code


def _job_still_running(props: dict[str, str]) -> bool:
    """Wake jobs use RemainAfterExit: a finished job parks in
    active(exited) — still 'active' to is-active, but the main process is
    gone. Running means the process itself is alive."""
    active, sub = props.get("ActiveState", ""), props.get("SubState", "")
    if props.get("LoadState") == "not-found":
        return False
    if active == "activating":
        return True
    return active == "active" and sub == "running"


def _wake_content(row: dict, log_tail: str) -> str:
    code = row.get("exit_code")
    code_s = "unknown" if code is None else code
    head = (
        f"[bg process {row['name']}] exit_code={code_s} "
        f"result={row.get('systemd_result') or 'unknown'}"
    )
    return (
        f"{head}\n{log_tail}\n\n"
        "This background process you started has finished. If it produced an "
        "artifact the user asked for, send it now. If it failed, tell the "
        "user plainly and decide whether to retry."
    )


async def _deliver_exit_wake(ctx: AppContext, row: dict) -> bool:
    """Deliver (or redeliver) the completion wake for a pending row."""
    from server.services.wake_service import wake_conversation

    parent = row.get("parent_session_key") or ""
    if not parent:
        return True  # nothing to wake — treat as delivered
    ws = ctx.settings.harness.workspace_dir.expanduser().resolve()
    tail = _tail_file(ws / row["log"], 40) if row.get("log") else ""
    content = _wake_content(row, tail)
    # A human chat must HEAR how its job went: the wake speaks like a task
    # relay (final text posted; NO_REPLY stays silent). As wake_nudge the
    # reply was dropped as bookkeeping — 2026-10-09 Mike's DM: the AI Doom
    # hat mockup was finished and described, and never reached him.
    speaks = parent.startswith(("agent:main:whatsapp:", "agent:main:email:"))
    if speaks:
        content += ("\n\nYour final text is POSTED to this chat. Images or "
                    "files must go out with send_whatsapp_message(media_path=…) "
                    "— text alone can't attach anything, so never say "
                    "'attached' without sending it.")
    try:
        return await wake_conversation(
            ctx, parent, content,
            call_category="subagent_result",
            provenance="task_relay" if speaks else "wake_nudge",
            metadata={"bg_job_id": row["id"], "bg_job_name": row["name"]},
        )
    except Exception:
        logger.exception("bg wake delivery failed for %s", row["name"])
        return False


async def poll_bg_jobs(ctx: AppContext) -> None:
    """One watcher pass: deliver owed wakes, then record + deliver exits.

    Also the boot pass (first tick): rows still 'running' whose unit is
    gone are orphaned (reboot), pending deliveries are retried. Safe to
    run at any cadence; settle transitions are single-writer (this loop).
    """
    repo = BgJobsRepository(ctx.db)
    now = utcnow().isoformat()

    for row in await repo.pending_deliveries():
        if await _deliver_exit_wake(ctx, row):
            await repo.set_delivery(row["id"], "delivered", now)
            logger.info("bg job %s wake delivered (retry)", row["name"])

    for row in await repo.running_rows():
        if row.get("mechanism") == "systemd" and row.get("unit"):
            try:
                props = await _unit_state(row["unit"])
            except Exception:
                # A single hung/failed systemctl read must not abort the
                # whole pass (live since 2026-09-22: TimeoutError at the
                # 30s _run_cmd limit, ~4-5/day, user-session stalls) —
                # skip this row; the next pass retries it. Rows after it
                # still settle, and wakes owed by them still deliver.
                logger.warning(
                    "bg poll: unit read failed for %s (unit=%s); "
                    "retrying next pass",
                    row["name"], row["unit"], exc_info=True)
                continue
            if row["wake_on_exit"]:
                # jobs use RemainAfterExit: 'active(exited)' means finished
                running = _job_still_running(props)
                status, result, exit_code = (
                    _classify_unit_exit(props) if not running else (None, None, None))
            else:
                # daemons have no RemainAfterExit: 'active' means alive
                running = props.get("ActiveState") in ("active", "activating") \
                    and props.get("LoadState") != "not-found"
                if running:
                    continue
                if props.get("LoadState") == "not-found":
                    # --collect daemons unload on death (any cause incl.
                    # reboot) — 'orphaned' is reserved for wake jobs
                    status, result, exit_code = \
                        "exited", "collected (exit status unavailable)", None
                else:
                    status, result, exit_code = _classify_unit_exit(props)
        else:
            running = await _entry_alive(row)
            status, result, exit_code = ("exited", None, None) if not running \
                else (None, None, None)
        if running:
            continue
        await repo.mark_terminal(
            row["id"], status=status, exit_code=exit_code,
            systemd_result=result, now_iso=now)
        logger.info("bg job %s terminal: %s (%s, exit=%s)",
                    row["name"], status, result, exit_code)
        if row["wake_on_exit"] and row.get("unit"):
            # Release the parked RemainAfterExit unit: stop transitions it
            # out of active(exited); reset-failed unloads failed/transient
            # remains. Both are no-ops on an already-gone unit.
            await _run_cmd(["systemctl", "--user", "stop", row["unit"]])
            await _run_cmd(["systemctl", "--user", "reset-failed", row["unit"]])
        if row.get("keep_alive") and not row["wake_on_exit"]:
            await _revive(ctx, row, status=status, result=result)
        if row["wake_on_exit"]:
            await repo.set_delivery(row["id"], "pending", now)
            fresh = await repo.get(row["id"])
            if fresh is not None and await _deliver_exit_wake(ctx, fresh):
                await repo.set_delivery(row["id"], "delivered", now)
                logger.info("bg job %s wake delivered", row["name"])


async def _revive(ctx: AppContext, row: dict, *, status: str | None,
                  result: str | None) -> None:
    """Restart a dead keep-alive daemon under the same name and command —
    after a crash or a reboot (its transient unit is gone). At most
    _KEEP_ALIVE_MAX_STARTS starts per name per hour: past that it's a crash
    loop, so stop restarting and tell the owner once (no running row is
    left, so the watcher doesn't revisit it)."""
    repo = BgJobsRepository(ctx.db)
    name = row["name"]
    since = (utcnow() - _dt.timedelta(hours=1)).isoformat()
    starts = await repo.starts_since(name, since)
    why = f"{status or 'exited'}: {result or 'unknown'}"
    if starts >= _KEEP_ALIVE_MAX_STARTS:
        logger.warning("bg keep-alive: %s died again (%s) — %d starts in the last "
                       "hour, giving up", name, why, starts)
        await _alert_owner(ctx, row, f"{name} keeps dying ({starts} starts in the "
                           f"last hour, last {why}). I've stopped restarting it.")
        return
    job_id, message = await _start_core(
        ctx, name=name, command=row["command"],
        description=row.get("description") or "", wake=False, ttl_seconds=0,
        parent_session_key=row.get("parent_session_key") or "",
        source=row.get("source") or "bg_start", keep_alive=True)
    if job_id is None:
        logger.warning("bg keep-alive: restart of %s failed: %s", name, message)
        await _alert_owner(ctx, row, f"{name} died ({why}) and the automatic "
                           f"restart failed: {message.removeprefix('Error: ')}")
        return
    logger.warning("bg keep-alive: %s died (%s) — restarted (job %s)", name, why, job_id)


async def _alert_owner(ctx: AppContext, row: dict, headline: str) -> None:
    from server.services.effects import emit_and_deliver
    from server.services.steering import owner_contact
    try:
        owner = await owner_contact(ctx)
        phone = "".join(ch for ch in str((owner or {}).get("phone_number") or "")
                        if ch.isdigit())
        if not phone:
            logger.warning("bg keep-alive alert for %s: no owner phone", row["name"])
            return
        ws = ctx.settings.harness.workspace_dir.expanduser().resolve()
        tail = _tail_file(ws / row["log"], 8) if row.get("log") else ""
        text = f"⚠️ Background service down: {headline}"
        if tail:
            text += f"\n\nLast log lines:\n{tail[-1200:]}"
        await emit_and_deliver(
            ctx, kind="whatsapp_send", idempotency_key=f"bg_keep_alive:{row['id']}",
            payload={"chat_id": f"{phone}@s.whatsapp.net", "text": text})
    except Exception:
        logger.exception("bg keep-alive alert failed for %s", row["name"])


async def bg_wake_loop(ctx: AppContext, stop_event: asyncio.Event) -> None:
    """Dedicated lifespan loop (see main.py) — 5s cadence; the wake is one
    LLM turn downstream, so ExecStopPost-style push held no advantage."""
    while not stop_event.is_set():
        try:
            await poll_bg_jobs(ctx)
        except Exception:
            logger.exception("bg wake poll failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_WATCH_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


# --------------------------------------------------------------------------- #
# one-time legacy import
# --------------------------------------------------------------------------- #

async def import_legacy_registry(ctx: AppContext) -> tuple[int, int]:
    """Import alive entries from the pre-DB registry (once), then archive it.

    Returns (imported, skipped_dead). Idempotent by file rename: if the
    archive already exists, does nothing.
    """
    path = _legacy_registry_path(ctx)
    archive = path.with_name("processes.json.imported")
    if not path.is_file() or archive.exists():
        return 0, 0
    try:
        data = json.loads(path.read_text())
        entries = data.get("processes", {})
    except (json.JSONDecodeError, OSError):
        logger.warning("legacy bg registry unreadable; archiving anyway")
        entries = {}

    repo = BgJobsRepository(ctx.db)
    now = utcnow().isoformat()
    imported = skipped = 0
    for name, entry in entries.items():
        if not _NAME_RE.fullmatch(name):
            skipped += 1
            continue
        if await repo.get_running_by_name(name) is not None:
            skipped += 1
            continue
        if not await _entry_alive(entry):
            skipped += 1  # dead legacy entries are residue, not history worth rows
            continue
        await repo.create(
            name=name,
            command=entry.get("command", ""),
            unit=entry.get("unit"),
            mechanism=entry.get("mechanism", "setsid"),
            pid=entry.get("pid"),
            pid_start_time=entry.get("pid_start_time"),
            description=entry.get("description", ""),
            source="bg_start",
            wake_on_exit=False,
            parent_session_key="",
            log=entry.get("log", f".bg/logs/{name}.log"),
            now_iso=now,
        )
        imported += 1
    os.replace(path, archive)
    logger.info("legacy bg registry imported: %d live, %d skipped; archived to %s",
                imported, skipped, archive.name)
    return imported, skipped


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #

def make_process_tools(ctx: AppContext, session_key: str = ""):
    """Create the bg_* background-process tools bound to the given context.

    session_key binds wake jobs (bg_start wake=True) to the conversation
    that started them — that's who gets woken on exit.
    """

    @tool
    async def bg_start(
        name: str,
        command: str,
        description: str = "",
        wake: bool = False,
        ttl: int = 0,
        goal_id: str = "",
        keep_alive: bool = False,
    ) -> str:
        """Start a background process (server, watcher, daemon — or a one-shot
        job) that keeps running across tool calls AND bob-server restarts.
        Give it a stable lowercase name — the same name stops/inspects it
        later. The command runs via bash with the workspace as cwd and the
        same sandbox rules as the bash tool; output appends to a log readable
        with bg_logs. Returns an error if the process exits immediately —
        check bg_logs.

        wake=True marks a ONE-SHOT JOB: when it exits, THIS conversation is
        woken with the exit code and log tail (no polling needed). Job units
        stay queryable until the watcher has read their result.

        ttl (seconds, optional) caps the run via systemd RuntimeMaxSec —
        at the cap the job is killed and reported as timed out. 0 = no cap.

        Example: bg_start(name="bob-fm", command="python3 skills/radio/station.py")
        Example: bg_start(name="render", command="python skills/…", wake=True)

        keep_alive=True for a SERVICE that must stay up (servers, watchers,
        daemons a skill depends on): if it dies or the machine reboots it is
        restarted automatically, and Mike is alerted if it keeps dying.
        bg_stop still stops it for good. Not for one-shot jobs.

        goal_id= links the process to the goal it serves (implicit inside a
        goal room) so the goal's state lists it."""
        job_id, message = await _start_core(
            ctx, name=name, command=command, description=description,
            wake=wake, ttl_seconds=ttl, parent_session_key=session_key,
            source="bg_start", keep_alive=keep_alive,
        )
        if job_id:
            from server.services.goal_links import link_mechanism
            message += await link_mechanism(
                ctx, session_key, goal_id,
                f"bg process {name} (job {job_id}): {description or command[:120]}")
        return message

    @tool
    async def bg_status(name: str = "", all: bool = False) -> str:
        """Check background processes. With no name: lists every RUNNING
        process with pid, uptime and liveness (plus wake jobs whose
        completion wake is still owed). With a name: that process's latest
        row, alive or terminal. all=True lists recent history rows too,
        with exit codes (last 50)."""
        repo = BgJobsRepository(ctx.db)
        if name and not _NAME_RE.fullmatch(name):
            return "Error: invalid name."

        if name:
            rows = await repo.list(limit=50)
            rows = [r for r in rows if r["name"] == name]
            if not rows:
                return f"Error: no process named {name!r}. bg_status with no args lists all."
            rows = rows[:1]
        elif all:
            rows = await repo.list(limit=50)
        else:
            rows = await repo.running_rows()

        if not rows:
            return "No background processes running." if not all else \
                "No bg_jobs rows yet."

        lines = []
        running = 0
        for r in rows:
            alive = r["status"] == "running" and await _entry_alive(r)
            running += int(alive)
            if not name and r["status"] == "running" and not alive:
                continue  # list view: the watcher will terminal it next tick
            state = "running" if alive else r["status"]
            desc = f" — {r['description']}" if r.get("description") else ""
            extras = []
            if r.get("exit_code") is not None:
                extras.append(f"exit {r['exit_code']}")
            if r.get("wake_on_exit") and r["status"] == "running":
                extras.append("wake-on-exit")
            if r.get("keep_alive"):
                extras.append("keep-alive")
            if r.get("delivery") == "pending":
                extras.append("WAKE OWED")
            up = ""
            if alive:
                started = _dt.datetime.fromisoformat(r["started_at"])
                uptime = _dt.datetime.now().astimezone() - started
                up = f", up {str(uptime).split('.')[0]}"
            extra_s = f" ({', '.join(extras)})" if extras else ""
            lines.append(
                f"{r['name']}: {state}{extra_s} (pid {r.get('pid')}{up}{desc})\n"
                f"  command: {r['command']}\n  log: {r['log']}"
            )
        header = f"{running}/{len(rows)} running" if not all else f"showing {len(rows)} rows, {running} running"
        return f"{header}\n\n" + "\n\n".join(lines)

    @tool
    async def bg_logs(name: str, lines: int = 50) -> str:
        """Read the last N lines (default 50) of a background process's log.
        Use this to check on a process after bg_start or when it misbehaves —
        stdout and stderr both land in the log. Works for terminal rows too
        (history logs are kept)."""
        if lines < 1 or lines > 500:
            return "Error: lines must be between 1 and 500."
        if not _NAME_RE.fullmatch(name):
            return "Error: invalid name."
        path = _log_path(ctx, name)
        return _tail_file(path, lines) or "(log is empty)"

    @tool
    async def bg_stop(name: str) -> str:
        """Stop a background process started with bg_start/run_bg_process.
        The whole process tree is terminated cleanly (TERM, then KILL after
        a grace period) and the row is marked killed. No wake is sent — the
        conversation asking for the stop already knows."""
        if not _NAME_RE.fullmatch(name):
            return "Error: invalid name."
        repo = BgJobsRepository(ctx.db)
        row = await repo.get_running_by_name(name)
        if row is None:
            return f"{name}: was not running."
        outcome = await _terminate(row)
        await repo.mark_terminal(
            row["id"], status="killed", exit_code=None,
            systemd_result=None, now_iso=utcnow().isoformat())
        logger.info("bg_stop: %s — %s", name, outcome)
        return f"{name}: {outcome}."

    @tool
    async def bg_restart(name: str) -> str:
        """Restart a background process using its stored command: stops the
        running instance (if any), marks its row replaced, and starts a
        fresh one under the same name. Wake jobs restart as plain daemons
        unless they were wake jobs (wake flag is preserved)."""
        if not _NAME_RE.fullmatch(name):
            return "Error: invalid name."
        repo = BgJobsRepository(ctx.db)
        row = await repo.get_running_by_name(name)
        stored_command = stored_desc = stored_wake = None
        stored_keep = False
        if row is not None:
            stored_command, stored_desc = row["command"], row.get("description", "")
            stored_wake = bool(row["wake_on_exit"])
            stored_keep = bool(row.get("keep_alive"))
            outcome = await _terminate(row)
            await repo.mark_replaced(row["id"], utcnow().isoformat())
        else:
            last = await repo.latest_by_name(name)
            if last is None:
                return (f"Error: no process named {name!r} has ever been registered "
                        "here, so there's no stored command to restart. Start it "
                        f"with bg_start(name={name!r}, command=...) — if it's a "
                        "skill's server, use_skill that skill first: its doc has "
                        "the exact command and health check.")
            stored_command, stored_desc = last["command"], last.get("description", "")
            stored_wake = bool(last["wake_on_exit"])
            stored_keep = bool(last.get("keep_alive"))
            outcome = "was not running"
        # Re-run the start path with the stored command (bg_start is a Tool
        # instance here, so call through .handler).
        result = await bg_start.handler(name, stored_command, stored_desc, stored_wake, 0,
                                        keep_alive=stored_keep)
        return f"stopped ({outcome}); {result}"

    return [bg_start, bg_status, bg_logs, bg_stop, bg_restart]
