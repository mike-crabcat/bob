# Bob Docker Restructure Plan

Date: 2026-08-31 (revised same day: Docker is for *additional* instances, not a migration)
Status: draft, not started
Goal: collapse the `packages/bob-server/bob_server` Python-package layout into top-level `server/` and `ui/` folders, drop the PyPI/wheel distribution model, and make Docker a supported way to stand up **additional** Bob instances. The primary instance on this box **stays on systemd + uv**, unchanged in kind.

---

## Why

The installable-package model (`pip install bob-server`, hatch wheel, publish to PyPI, version pinning in a skill) bought nothing: the primary deployment is this box running from a checkout, the wheel carries the built SPA and voice frontend as package data, and the layout costs an extra three directory levels for no benefit. A Docker image gives a self-contained artifact for spinning up extra instances (staging, a second Bob elsewhere) without touching how prod runs.

## Non-goals

- Migrating the primary instance to Docker — `bob.service`, `whatsappbridge.service`, and the media-cleanup timer keep running as they do today.
- Any change to prod URLs, webhook endpoints, or the funnel.

## What the review found (current state)

- **Layout**: everything lives in `packages/bob-server/bob_server/` (187 `.py` files: `routers/`, `services/`, `cli/`, `repositories/`, `schemas/`, `evals/`, `voice_frontend/`). The SPA source is `bob_server/ui_app/` (Vite + React + TS), building to `../ui_dist` — i.e. *inside the Python package* — and `main.py:270` serves it via `Path(__file__).parent / "ui_dist"`.
- **Distribution today**: `scripts/publish.sh` / `scripts/rollout.sh` build a hatch wheel and upload to PyPI/TestPyPI. `packages/bob-server/dist/` holds stale `cyborg_server-0.4.13` wheels from before the rename. `publish.sh` contains a **plaintext PyPI token** (gitignored, but on disk — revoke it in cleanup regardless).
- **Deploy today**: `deploy.sh` = run package test suite → `git push origin bobv3` (stale branch ref; real work lands on `master`) → `systemctl --user restart bob.service` → healthcheck `GET /dashboard` (307/200). The systemd unit runs `uv run bob serve …` with `WorkingDirectory=…/ui_app`. **`deploy.sh` never builds the SPA** — whatever `ui_dist` was last built by hand is what prod serves. The new deploy flow must fix this.
- **Host-side companions**: `whatsappbridge.service` (Go binary from `services/whatsappbridge/bin`), `whatsapp-media-cleanup.timer`, `openclaw-gateway.service` (separate software, not in this repo — primary's LLM traffic routes to it on localhost). The radio (`bob-fm` unit) is host-side and calls the API on `127.0.0.1:8420`.
- **The claude CLI constraint**: `subagent_service._run_claude` spawns the `claude` CLI as a subprocess (`shutil.which("claude")` or `~/.local/bin/claude`) with `cwd` in the workspace, using `--session-id`/`--resume` and reading/writing `~/.claude` (auth, transcripts, memory). Skills pip-install into a harness venv (`~/bobenv`, created on demand by `_ensure_venv`). **A Bob container cannot be a slim python image** — it is an agent image: Python + Node + the claude CLI + git + venv tooling, with per-instance volumes for data/config/workspace/`~/.claude`/`~/bobenv`.
- **Repo bloat**: `ui_app/node_modules` is **tracked in git — 21,361 files** of the 21,945 total. Also present: two lockfiles (`package-lock.json` *and* `pnpm-lock.yaml`), a stray `bob_server/db.sqlite`, and two `uv.lock` files (root workspace + package).
- **Tests are split**: `packages/bob-server/tests/` (the deploy gate runs these) and root `tests/` (older; its `conftest.py` hardcodes `SCHEMA_DIR` through `packages/bob-server/...`). Both import `bob_server`.
- **Runtime paths** (per instance, volumes for containers): `~/data` (SQLite + WAL, bridge db/media, recordings, api_secret), `~/config` (`.env`, `models.yaml`, key files), `~/workspace` (agent sandbox, skills + secrets), `~/.claude`, `~/bobenv`.
- **Env plumbing already exists**: every path and toggle is `BOB_*` env-overridable (`config.py`), including `BOB_HOST`/`BOB_PORT`, and `.env` loading from the config dir. The container needs no code changes here — bind `0.0.0.0`, publish the port.

## Target layout

```
bob/
  server/                  # was packages/bob-server/bob_server — the FastAPI app, plain source dir
    main.py config.py database.py models.py ...
    routers/ services/ cli/ repositories/ schemas/ evals/ voice_frontend/
    ui_dist/               # build output (gitignored; written by `npm run build` in ui/, baked into the image)
  ui/                      # was packages/bob-server/bob_server/ui_app — Vite + React SPA source
    src/ public/ index.html vite.config.ts package.json package-lock.json
  bridge/                  # was services/whatsappbridge — Go WhatsApp bridge
  tests/                   # merged: root tests/ + packages/bob-server/tests/
  Dockerfile compose.yaml .dockerignore docker/   # the additional-instance path
  pyproject.toml           # single project: deps merged from root workspace + package, `bob` script entry
  uv.lock                  # single lockfile
  deploy.sh backup.sh README.md AGENTS.md DOCS.yaml docs/ ...
```

Gone: `packages/`, the package-level `pyproject.toml`/`uv.lock`, `dist/`, `scripts/`, the hatch build config and wheel-includes list, the `[tool.uv.sources]` editable hook.

## Decisions

**D1 — Import package name: rename `bob_server` → `server` (recommended).**
The folder the user asked for and the import package must share a name unless we nest (`server/bob_server/`). Renaming touches ~154 package files + ~84 test files, but it is a mechanical `sed 's/\bbob_server\b/server/g'` over `*.py` plus a short hand-fix list (pyproject entry point `bob = "server.cli:app"`, root conftest `SCHEMA_DIR`, docs), fully verified by the merged test suite and a boot. This matches the intent — no package pretence left. Fallback if the churn scares us at review time: keep `server/bob_server/` nesting and zero code churn; decide at Phase 1 kickoff, not later.

**D2 — Container networking: bridge network + published port, parameterized.**
Additional instances are exactly the case where host networking is wrong — on this box the primary already owns 8420, and a second host-network container would fight it. The compose file publishes `${BOB_PORT:-8420}` and reaches host services (openclaw gateway, when co-located) via `host.docker.internal` (`extra_hosts: host.docker.internal:host-gateway`) or a tailnet address in the instance's `.env` — `base_url` is already env-driven. Voice/phone webhooks need a public URL per instance (funnel or ngress); an instance without one just runs with phone disabled.

**D3 — UI package manager: npm.** `package-lock.json` exists and node_modules on disk is npm-laid-out; delete `pnpm-lock.yaml`. `npm ci` in the image build.

**D4 — Image distribution: build from a checkout per instance; registry later.** Each instance host clones the repo and `docker compose build`. Pushing to ghcr (the gh PAT has write:packages) is a follow-up for hosts that shouldn't build; don't block on it.

**D5 — Browser skill / Chrome: deferred.** The browser skill drives a bob-owned headless Chrome on CDP 9223 via the browser-use CLI inside the agent's bash. In a container that means Chrome in the bob image (+~400 MB) or a companion chrome service. Not in the initial image — Phase 4 item; container instances run with the browser skill degraded until then (the systemd primary is unaffected).

**D6 — Per-instance claude auth.** Each container instance gets its own named volume for `~/.claude` and authenticates once at creation: either run `docker compose run --rm bob claude login` (interactive OAuth) or set `ANTHROPIC_API_KEY` in the instance `.env`. Sharing the host's `~/.claude` into a container is possible on this box (transcript slugs are cwd-derived so they don't collide) but couples instances' claude state — not the default.

## Phase 0 — Preconditions

1. Land the in-flight `steering` work: 3 modified files uncommitted (`config.py`, `claim_router.py`, `test_claim_router.py`), branch committed but unpushed. Merge to `master` first so the restructure diff sits on a clean base.
2. Hygiene commit (separate, mechanical, reviewed by diff-stat only):
   - `git rm -r --cached packages/bob-server/bob_server/ui_app/node_modules` (−21,361 files) and gitignore `node_modules/`.
   - Delete `packages/bob-server/dist/` (stale cyborg wheels), stray `bob_server/db.sqlite`, `pnpm-lock.yaml`.
   - This shrinks every later diff and the Docker build context.

## Phase 1 — Restructure (primary keeps deploying via systemd, permanently)

1. Moves (all `git mv` to preserve history):
   - `packages/bob-server/bob_server` → `server/`
   - `server/ui_app` → `ui/` (node_modules already untracked)
   - `packages/bob-server/tests/*` → `tests/` (merge with root suite; reconcile the two `conftest.py` — package one owns asyncio fixtures, root one owns the `db` fixture and `SCHEMA_DIR`)
   - `services/whatsappbridge` → `bridge/` (Makefile is path-relative; verify `make build`)
   - delete `packages/`, package `pyproject.toml`, package `uv.lock`, `scripts/`
2. Import rename (D1): sed `bob_server` → `server` across `*.py`; then `git grep -n 'bob_server'` and hand-clear the remainder (docs, DOCS.yaml, README; CHANGELOG historical entries stay as-is). `git grep bob_server server/schemas/` must come back empty before proceeding — nothing rename-shaped may leak into stored data.
3. `pyproject.toml` (root, single project): merge dependency lists (package deps + root's pillow/qrcode), dev groups (pytest, pytest-asyncio, pytest-timeout, pytest-cov), `[project.scripts] bob = "server.cli:app"`, drop hatch/uv-sources/wheel sections; single pytest config: `testpaths = ["tests"]`, `asyncio_mode = "auto"`, keep the `openclaw_live` marker. Regenerate `uv.lock`; delete and re-create `.venv`.
4. `ui/vite.config.ts`: `outDir: "../server/ui_dist"` (server code unchanged — still `Path(__file__).parent / "ui_dist"`).
5. `.gitignore`: `node_modules/`, `server/ui_dist`, `bridge/bin`, `bridge/whatsappbridge`; drop the `scripts/` entries.
6. Primary deploy path (systemd, now the permanent arrangement):
   - `bob.service`: `WorkingDirectory=/home/bob/bob`, `ExecStart=/home/bob/.local/bin/uv run --project /home/bob/bob bob serve --host 127.0.0.1 --port 8420 --data-dir /home/bob/data --config-dir /home/bob/config --db-path /home/bob/data/bob.db`
   - `deploy.sh`: fix stale `bobv3` → `master`; run the **full merged suite** (`uv run pytest tests -q`); **add the missing UI build step** (`npm --prefix ui ci && npm --prefix ui run build`) before restart — closing the "prod serves a stale SPA" gap.
   - `backup.sh`: update its `packages/bob-server` path references.
7. Docs: rewrite the layout/dev sections of `AGENTS.md` (it feeds CLAUDE.md), README Setup, `DOCS.yaml`, `docs/datamodel.md`. `CHANGELOG.md`: new entry, don't rewrite history.
8. Verification gate (all before touching prod): full pytest suite green; `bob serve` boots from new layout; dashboard + smoke items from `docs/smoke-test-guide.md` pass; one real WhatsApp inbound and one dashboard action observed in logs.

## Phase 2 — Dockerize the additional-instance path

1. **Dockerfile** (repo root), multi-stage:

```dockerfile
# ---- ui build ----
FROM node:22-bookworm-slim AS ui
WORKDIR /build
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
RUN npx vite build --outDir dist   # host build writes ../server/ui_dist; image build keeps it stage-local

# ---- python deps ----
FROM python:3.12-slim-bookworm AS deps
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# ---- runtime (agent image — see the claude CLI constraint) ----
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
      nodejs npm git curl ca-certificates tzdata \
 && npm i -g @anthropic-ai/claude-code@<pinned> \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=deps /app/.venv /app/.venv
COPY server/ /app/server/
COPY --from=ui /build/dist/ /app/server/ui_dist/
ENV PATH="/app/.venv/bin:$PATH" HOME=/home/bob TZ=${TZ:-UTC}
RUN useradd -u 1000 -m bob
USER bob
CMD ["bob", "serve", "--host", "0.0.0.0", "--port", "8420", \
     "--data-dir", "/home/bob/data", "--config-dir", "/home/bob/config", \
     "--db-path", "/home/bob/data/bob.db"]
```

   Notes: node from apt is 18.x — fine for the claude CLI (18+); pin the CLI version and bump deliberately. `python:3.12-slim` ships ensurepip, so `_ensure_venv`'s `~/bobenv` creation works; skills that compile C extensions would need gcc — add only when a skill actually fails. sqlite-vec/PyAV/soundfile/numpy all ship manylinux wheels. Bind `0.0.0.0` inside the container; the published port is the instance's concern (D2). A `.dockerignore` (`.git`, `ui/node_modules`, `.venv`, `tests`, `docs`, data dirs) keeps the context small even after the node_modules purge.

2. **`bridge/Dockerfile`**: `golang:1.2x-bookworm` build stage → `debian:bookworm-slim` + ca-certificates runtime (or distroless/static if the binary is static). Reads config via `BOB_CONFIG_DIR`, connects to Bob's ws URL — parameterized per instance, not hardcoded 127.0.0.1.

3. **`compose.yaml`** (repo root) — parameterized so N instances coexist on one box or stand alone:

```yaml
services:
  bob:
    build: .
    ports:
      - "${BOB_PORT:-8420}:8420"
    extra_hosts:
      - "host.docker.internal:host-gateway"   # reach a co-located openclaw gateway
    user: "1000:1000"
    env_file: ${BOB_INSTANCE_DIR}/config/.env
    volumes:
      - ${BOB_INSTANCE_DIR}/data:/home/bob/data
      - ${BOB_INSTANCE_DIR}/config:/home/bob/config
      - ${BOB_INSTANCE_DIR}/workspace:/home/bob/workspace
      - claude:/home/bob/.claude
      - bobenv:/home/bob/bobenv
    restart: unless-stopped

  whatsappbridge:
    build: ./bridge
    profiles: ["whatsapp"]                     # opt-in — only instances with their own number
    env_file: ${BOB_INSTANCE_DIR}/data/whatsappbridge/.env
    environment: { BOB_WS_URL: "ws://bob:8420/ws" }
    volumes:
      - ${BOB_INSTANCE_DIR}/data:/home/bob/data
      - ${BOB_INSTANCE_DIR}/workspace:/home/bob/workspace
    restart: unless-stopped

volumes:
  claude:
  bobenv:
```

   Run per instance: `BOB_INSTANCE_DIR=~/instances/bob2 BOB_PORT=8421 docker compose -p bob2 up -d`. The `-p` project name keeps `claude`/`bobenv` volumes per instance. Nothing here touches the primary's systemd units or its `~/data`/`~/config`/`~/workspace`.

4. **First instance = the burn-in** (per the careful-rollouts rule — never ship an image on faith): stand up `bob-staging` on this box with `BOB_PORT=8421` and a `backup.sh` snapshot restored into `~/instances/staging/`. Verify: dashboard renders on 8421, a stub conversation turn end-to-end (dashboard → llm_call_log → reply), `claude` CLI spawns inside the container (`docker compose exec bob claude --version`), heartbeat tasks fire with sane wall-clock under `TZ`, healthcheck curl. The primary runs untouched throughout — a bad staging container is a `docker compose down`, not an incident.

## Phase 3 — Standing up additional instances (runbook, lives in README)

1. On the host: clone repo, `docker compose build`.
2. Create the instance root: `mkdir -p ~/instances/<name>/{data,config,workspace}`; write `config/.env` (LLM base_url/keys — for a co-located gateway use `http://host.docker.internal:<port>`; leave phone disabled unless the instance has its own Twilio number + public URL; whatsapp only with its own number, then `--profile whatsapp`).
3. Claude auth (D6): `docker compose run --rm bob claude login`, or `ANTHROPIC_API_KEY` in the instance `.env`.
4. `BOB_INSTANCE_DIR=… BOB_PORT=<free port> docker compose -p <name> up -d`; healthcheck `GET /dashboard` on the published port.
5. Same-box port allocation: primary owns 8420 (+ 8430 bridge, 8443 funnel); instances take 8421+.
6. What is per-instance vs shared: everything under `BOB_INSTANCE_DIR` plus the `claude`/`bobenv` volumes is per-instance; nothing is shared with the primary by default.

## Phase 4 — Cleanup and follow-ups

- Revoke the PyPI token in `publish.sh` before deleting the file (dead distribution path; token shouldn't outlive it).
- Update agent memory entries that reference `packages/bob-server/...` paths.
- Chrome for the browser skill in containers (D5): bake chromium into the image or a companion chrome service exposing CDP; pick after checking how browser-use launches/connects.
- Optional: push images to ghcr so instance hosts don't build; secrets-from-files for remaining env-var secrets (Twilio creds still leak to agent bash — API-security point 2; a read-only secrets mount matches the existing OpenRouter key-file pattern).
- Optional: an instance-pruning/cleanup cron for media dirs on long-lived containers (the systemd timer only covers the primary's workspace).

## Risks / open items

| Risk | Mitigation |
|---|---|
| Import rename misses a non-`.py` reference | `git grep -n bob_server` empty-gate in Phase 1.2; full suite + boot gate |
| claude CLI auth/behaviour differs in container | Phase 2.4 exec test; per-instance volume with a documented login step (D6) |
| Two instances, one box: port/volume collisions | `BOB_PORT` + `BOB_INSTANCE_DIR` + `-p` project names; primary's ports documented as taken |
| Instance env leaks secrets to agent bash | Same exposure class as the primary today; Phase 4 file-secrets item improves it |
| Timezone/locale drift breaks wall-clock tasks | `TZ` env per instance (default UTC, document setting it), tzdata installed, heartbeat watch item |
| Root-owned files in bind mounts | `user: "1000:1000"` + ownership check in the runbook |
| Two test suites merge conflicts (fixtures, markers) | Reconcile conftests explicitly in Phase 1.3; suite must be green pre-deploy either way |
| WhatsApp/phone in a container without its own number/funnel | Bridge behind a compose profile; phone gated on `base_url` config — documented in the runbook |
