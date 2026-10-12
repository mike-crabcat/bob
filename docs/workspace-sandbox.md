# Workspace boundary: file tools and the bash sandbox

How Bob's own tools are confined to his workspace (live since 2026-10-13).

## Persona

The system prompt renders the persona straight from the repo bundle
(`self/<name>/*.md`, `services/persona.py`); `user.md` is the one
workspace-owned file. The boot heal still mirrors the bundle into
`workspace/self/` as a read-only *view* for skills (avatar canon refs,
reaction clips) — defacing it changes nothing the model is told, and the
file tools refuse to write it.

## File tools (`services/file_tools.py`)

| Tool | What it does |
|---|---|
| `read_file(path, offset, limit)` | Numbered lines, total-line header with a paging hint, line/char caps; media → `read_image`/`read_video` |
| `edit_file(path, old, new, replace_all)` | Exact match only; 0 matches → closest lines; >1 → their line numbers; atomic write keeping mode + line endings; returns the unified diff |
| `multi_edit(path, edits[])` | Ordered, all-or-nothing, one combined diff |

Paths go through `workspace_tools._resolve_path` / `_resolve_writable`:
resolved (symlinks followed) before the containment check, and `self/` is
never writable. Kill switch `BOB_FILE_TOOLS=off`. Evals: the
`workspace_tools` category (surgical edit in a 3,000-line file, four-site
rename, quote-heavy block replace) — all outcome-gated.

## Bash sandbox (`services/bash_sandbox.py` + `services/landlock_exec.py`)

`BOB_BASH_SANDBOX=landlock` (in `~/config/.env`; `off` + restart to roll
back). Each bash call execs through `landlock_exec.py` — a stdlib launcher
run with `/usr/bin/python3` that restricts only that child tree (~20 ms):

| Layer | Enforces |
|---|---|
| Landlock fs | rw: workspace, skill venv, `/tmp/bob-sandbox` (`TMPDIR`), `/dev/null` etc. ro: system dirs, Linuxbrew, `~/.local/bin`, Claude Code, `/opt/google`, + `BOB_BASH_SANDBOX_RO` extras. Everything else denied: `~/data` (bob.db), `~/config`, `~/.ssh`, shared `/tmp` |
| Landlock net | TCP connect allowlist (`_DEFAULT_PORTS`, override `BOB_BASH_SANDBOX_PORTS`). Never: 8420 Bob API, 8430 bridge, 8443/5173 dashboard, 18789/18791 OpenClaw, 11434 ollama |
| Landlock scope | signals + abstract unix sockets confined — can't signal `bob.service` or read its `/proc/<pid>/environ` |
| seccomp | `socket(AF_UNIX)` → EACCES, io_uring → ENOSYS. Landlock can't govern pathname unix-socket connects on this kernel; this blocks `systemd-run --user`, `systemctl --user`, docker, tailscale, ssh-agent |
| NO_NEW_PRIVS | setuid binaries can't elevate (sudo is dead) |
| env | allowlist only: locale/TZ, the four mapped skill API keys, `BOB_WORKSPACE_DIR`; fixed PATH + `VIRTUAL_ENV` (skill venv); caches redirected into `workspace/.cache` |

Startup logs the live mode (`bash sandbox: landlock ABI 8 …`); if
Landlock is unavailable (Docker seccomp, old kernel, Mac VM) bash degrades
**loudly** to the regex filter. Docker's default seccomp allows Landlock.

What it deliberately costs: launching Chrome from bash (use the managed
browser instance), `systemctl --user` from skills (use `bg_start`), shared
`/tmp` paths (use `scratch/` or `$TMPDIR`), snaps (need setuid).

### Not yet covered

- `bg_start` / `run_bg_process` jobs run via `systemd-run` from the server,
  outside the sandbox (they do get an env allowlist).
- Unsandboxed daemons (perth, radio, cryptobro, frigate …) run code from
  the writable workspace — editing it is a delayed escape.
- UDP isn't filtered (DNS, SSDP).
- The regex preflight (`_check_command_safety`) still runs; after a clean
  burn-in it shrinks to the behaviour guards (inline-sleep redirect, bash
  syntax check). DB-client / path rules become redundant.

Tests: `tests/services/test_file_tools.py`, `tests/services/test_bash_sandbox.py`
(real kernel; skipped where Landlock is unavailable).
