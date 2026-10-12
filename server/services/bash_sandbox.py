"""Kernel sandbox for Bob's bash tool (Landlock, 2026-10-12).

The regex preflight (``workspace_tools._check_command_safety``) is
bypassable by construction. Landlock enforces the workspace boundary in
the kernel instead: each bash call execs through ``landlock_exec.py``,
which restricts only that child tree — the server stays unconfined.

Policy (built from settings so the systemd primary and Docker instances
share it):
- read+write: the workspace, the shared skill venv, /dev/null|tty
- read-only: system + toolchain dirs (see ``_RO_SYSTEM`` / extras)
- nothing else: ~/data (bob.db), ~/config (secrets), ~/.ssh, rest of $HOME
- TCP connect: an allowlist of ports — Bob's own API port is never in it
- scope: signals/abstract unix sockets confined (can't signal bob.service)

Mode: ``BOB_BASH_SANDBOX=landlock|off`` (default off until the replay
gate passes). If Landlock is unavailable (Docker seccomp, old kernel, a
Mac VM) the tool degrades LOUDLY to the regex filter — startup logs which
mode is live. Verified on this box: ABI 8; bob.db, ~/config/.env,
/proc/<bob>/environ, kill(bob), :8420 all denied; ~20 ms per call.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LAUNCHER = Path(__file__).with_name("landlock_exec.py")
# System python, not the server's venv/Linuxbrew one: its stdlib lives
# under /usr, which the policy grants read-only.
LAUNCHER_PYTHON = "/usr/bin/python3"

_RO_SYSTEM = (
    "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32",
    "/etc", "/proc", "/sys", "/dev",
    "/run/systemd/resolve",          # /etc/resolv.conf points here (DNS)
    "/var/cache/fontconfig",
    "/opt/google",                    # chrome binary (browser skill)
    "/home/linuxbrew/.linuxbrew",     # ffmpeg/ffprobe/sqlite3 + bobenv's python
)
_RW_DEVICES = ("/dev/null", "/dev/zero", "/dev/tty", "/dev/pts", "/dev/shm")

# TCP connect allowlist (2026-10-12 skills audit). 80/443 web; 9223 Bob's
# headless Chrome CDP; 9222 the operator's Chrome CDP (browser skill uses
# it today); local skill daemons: 8460 perth, 8791 cryptobro, 8017
# aus-legal, 8787/8788 share-files, 8010/8012 radio, 5000 frigate; 1400
# Sonos (LAN). Deliberately ABSENT: 8420 Bob's API, 8430 the WhatsApp
# bridge, 8443/5173 dashboard, 18789/18791 OpenClaw, 11434 ollama.
# Override with BOB_BASH_SANDBOX_PORTS="80,443,…". Bob's own API port is
# removed even if listed.
_DEFAULT_PORTS = (80, 443, 9223, 9222, 8460, 8791, 8017, 8787, 8788,
                  8010, 8012, 5000, 1400)

# Private scratch dir, exported as TMPDIR — shared /tmp holds other
# processes' files (agent transcripts, eval worlds), so it isn't granted.
SANDBOX_TMP = Path("/tmp/bob-sandbox")

# Env the sandboxed shell gets. Everything else is dropped: config.py
# loads ~/config/.env into os.environ, so the old inherit-everything env
# handed bash every secret (Twilio, BOB_STIMULUS_TOKEN, bridge token, …).
# The four mapped API keys stay — skills use them by design.
_ENV_KEEP = (
    "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "USER", "LOGNAME",
    "BOB_WORKSPACE_DIR", "BOB_WORKSPACE",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "AGENTMAIL_API_KEY",
    "GOOGLE_PLACES_API_KEY", "GIPHY_API_KEY",
)


def mode() -> str:
    raw = os.getenv("BOB_BASH_SANDBOX", "off").strip().lower()
    return "landlock" if raw == "landlock" else "off"


@lru_cache(maxsize=1)
def landlock_abi() -> int:
    """Landlock ABI via the launcher's --probe (0 = unavailable)."""
    if not (Path(LAUNCHER_PYTHON).exists() and LAUNCHER.exists()):
        return 0
    try:
        out = subprocess.run(
            [LAUNCHER_PYTHON, "-I", "-S", str(LAUNCHER), "--probe"],
            capture_output=True, text=True, timeout=10)
        return int(out.stdout.strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def active() -> bool:
    return mode() == "landlock" and landlock_abi() >= 1


def _extra(env_key: str) -> list[str]:
    return [p for p in os.getenv(env_key, "").split(":") if p.strip()]


def policy(settings: Any) -> dict[str, Any]:
    home = Path.home()
    workspace = str(settings.harness.workspace_dir.expanduser().resolve())
    venv = str(settings.harness.venv_dir.expanduser())
    rw = [workspace, venv, str(SANDBOX_TMP), *_RW_DEVICES,
          *_extra("BOB_BASH_SANDBOX_RW")]
    # Machine-specific read-only extras (e.g. the workspace docs symlink
    # targets) ride BOB_BASH_SANDBOX_RO="/a:/b". Workspace symlink targets
    # are NEVER auto-granted — Bob could plant one pointing at ~/config.
    ro = [*_RO_SYSTEM,
          str(home / ".local" / "bin"),
          str(home / ".local" / "share" / "claude"),
          str(home / ".gitconfig"),
          *_extra("BOB_BASH_SANDBOX_RO")]
    raw_ports = os.getenv("BOB_BASH_SANDBOX_PORTS", "")
    ports = ([int(p) for p in raw_ports.split(",") if p.strip().isdigit()]
             if raw_ports.strip() else list(_DEFAULT_PORTS))
    own = int(getattr(settings, "port", 8420) or 8420)
    ports = sorted({p for p in ports if p != own})
    return {"rw": rw, "ro": ro, "tcp_connect": ports, "scope": True,
            "seccomp": True}


def wrap(argv: list[str], settings: Any) -> list[str]:
    """argv for a sandboxed exec, or argv unchanged when not active."""
    if not active():
        return argv
    SANDBOX_TMP.mkdir(mode=0o700, exist_ok=True)
    return [LAUNCHER_PYTHON, "-I", "-S", str(LAUNCHER),
            "--policy", json.dumps(policy(settings)), "--", *argv]


def bash_argv(command: str, settings: Any) -> list[str]:
    """The bash tool's argv: sandboxed shells skip rc files (~/.bashrc is
    outside the policy — every call would warn)."""
    if active():
        return wrap(["bash", "--norc", "--noprofile", "-c", command], settings)
    return ["bash", "-c", command]


def clean_env(env: dict[str, str], settings: Any) -> dict[str, str]:
    """Allowlist-only env for a sandboxed shell; unchanged when inactive.
    Caches/config are redirected into the workspace (HOME's are denied)."""
    if not active():
        return env
    workspace = settings.harness.workspace_dir.expanduser().resolve()
    out = {k: env[k] for k in _ENV_KEEP if env.get(k)}
    # A fixed PATH, not the service's: bob.service runs under `uv run`, so
    # its PATH leads with the repo .venv (ungranted) — python3 would resolve
    # there and die. Skill venv first, then the granted toolchain dirs.
    home = Path.home()
    venv = settings.harness.venv_dir.expanduser()
    has_venv = (venv / "bin" / "python").exists()
    path = [*([str(venv / "bin")] if has_venv else []),
            str(home / ".local" / "bin"), "/usr/local/bin", "/usr/bin", "/bin",
            "/home/linuxbrew/.linuxbrew/bin"]
    out.update({
        "PATH": ":".join(path),
        "HOME": str(Path.home()),
        "SHELL": "/bin/bash",
        "TMPDIR": str(SANDBOX_TMP),
        "XDG_CACHE_HOME": str(workspace / ".cache"),
        "XDG_CONFIG_HOME": str(workspace / ".config"),
        "PIP_CACHE_DIR": str(workspace / ".cache" / "pip"),
        "npm_config_cache": str(workspace / ".cache" / "npm"),
        "MPLCONFIGDIR": str(workspace / ".cache" / "matplotlib"),
    })
    if has_venv:   # only ever the skill venv — never the service's own
        out["VIRTUAL_ENV"] = str(venv)
    return out


def log_startup_mode() -> None:
    m, abi = mode(), landlock_abi()
    if m == "landlock" and abi >= 1:
        logger.info("bash sandbox: landlock ABI %d (kernel-enforced)", abi)
    elif m == "landlock":
        logger.warning("bash sandbox: LANDLOCK REQUESTED BUT UNAVAILABLE — "
                       "falling back to the regex filter only")
    else:
        logger.info("bash sandbox: off (regex filter only; landlock ABI %d "
                    "available)", abi)
