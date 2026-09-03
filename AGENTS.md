# Bob - AI Agent Platform

## Architecture

A single FastAPI application plus a Go WhatsApp bridge. Distributed via Docker (see `docs/bob-docker-plan.md`); the primary instance on this box runs from this checkout under systemd.

## Database

- **Engine**: SQLite via `aiosqlite` with WAL journal mode and foreign keys enabled
- **Default location**: `~/data/bob.db`
- **Override**: Set `BOB_DB_PATH` environment variable
- **Data directory**: `~/data/` (override with `BOB_DATA_DIR`)
- **Config directory**: `~/config/` (override with `BOB_CONFIG_DIR`)
- **Connection pool**: 4 connections by default (override with `BOB_DB_POOL_SIZE`)
- **Migrations**: SQL files in `server/schemas/`, numbered and applied automatically on startup via `apply_migrations()`

## Server

- **Default host/port**: `127.0.0.1:8420` (override with `BOB_HOST` / `BOB_PORT`)
- **Entry point**: `server/main.py`
- **Dashboard**: React SPA (`ui/`, Vite + TypeScript + Tailwind). Outside docker the dashboard is the vite dev server (`npm --prefix ui run dev`); the docker image bakes a build into top-level `ui_dist/` which the server mounts at `/dashboard` when present.
- **API**: REST endpoints under `/api/v1/` plus `/dashboard/api/*` for the SPA

## Key Directories

- `server/` - FastAPI app (plain source dir, import package `server`)
  - `routers/` - FastAPI routers (dashboard_api is itself a package split by domain)
  - `services/` - Domain services (dispatch, memory, realtime bridge, subagents, …)
  - `cli/` - Typer CLI split by subapp (`bob serve`, `bob dream`, …)
  - `repositories/` - one module per table family (SQL-ownership rule)
  - `schemas/` - SQL migration files
  - `voice_frontend/` - static voice/realtime session pages
  - `models.py`, `config.py`, `database.py`, `heartbeat.py`, …
- `ui/` - Dashboard SPA source (Vite + TypeScript + Tailwind), builds to `server/ui_dist/`
- `bridge/` - Go WhatsApp bridge (`make build` → `bridge/bin/whatsappbridge`)
- `skills/` - core skill bundle captured from the live workspace (seeded into fresh instances; `scripts` per `docs/bob-docker-plan.md`)
- `self/` - persona + avatar bundle (`self/bob/`: soul/identity/agents markdown + avatar canon refs and reaction clips; plus `user.md` boilerplate at the repo root). Single source of truth for the persona — at every boot `services/self_bundle.py` heals it into `workspace/self/bob/` (changed files restored, extras pruned, read-only bits). Git history is the persona history; the dashboard persona editor and `persona_records` API are retired (the DB table sits dormant as an archive of revisions 1-10). `workspace/user.md` is the one persona-ish file an instance owns: seeded only-if-missing, then its own (the owner profile).
- `tests/` - pytest suite (deploy gate; `tests/legacy/` is quarantined, uncollected)
- `docs/` - design docs, plans, datamodel reference

## Phone & voice

All realtime voice (Twilio phone calls and browser voice-link sessions) runs the OpenAI Realtime bridge in `services/realtime_bridge.py` — audio-source-agnostic, so the free browser harness (`/voice/realtime`) predicts phone behaviour. Key modules:

- `services/voice_dispatch_service.py` — single owner of call placement: instruction builders, modality alias table, Twilio placement, hangup, completion helpers. Nothing imports call placement from routers. Outbound calls prewarm the OpenAI session while the phone rings (`services/realtime_prewarm.py` registry), so the media stream attaches to a live, fully-configured session at answer — the callee's greeting must not ride a setup-backlog burst into a half-configured one.
- `routers/phone.py` — Twilio webhooks + the media-stream bridge lifecycle (partial transcripts per turn, structured outcomes, recordings).
- `services/voice_session_service.py` — browser voice-link tokens/lifecycle; mirrors rows into `phone_calls` so links appear in the calls UI.
- Dispatch entry point for the LLM: `create_subagent(agent_type="openai_voice", modality="phone"|"voice_link")`.
- Schema and data flow: see `docs/datamodel.md` → Phone Calls & Voice Sessions. Note `services/voice_service.py` + `/voice/ws` are the LEGACY local STT→TTS pipeline (language-practice frontend only) — do not build on them.

## Runtime paths

The database is at `/home/bob/data/bob.db`
Fetch logs for the running service using `journalctl` command e.g. `journalctl --user -u bob.service --since "10 min ago"  # recent`
The workspace directory is `/home/bob/workspace`
Config directory is `/home/bob/config`

## Development

- Package manager: `uv` (Python 3.12 pinned via `.python-version`); single project at the repo root
- UI: `npm` in `ui/` — the dashboard outside docker is the vite dev server, run as the `bob-ui.service` user unit (auto-starts, logs: `journalctl --user -u bob-ui.service`)
- Deploy (primary): `systemctl --user restart bob.service` — the unit runs `uv run` from this checkout, so a restart picks up the working tree. Run `uv run pytest tests -q` yourself before meaningful changes; nothing gates it automatically
- Docker instances: see README → "Bob instances (Docker)"
- Test: `uv run pytest tests -q`
