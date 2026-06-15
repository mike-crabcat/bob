# Bob - AI Agent Platform

## Architecture

This is a Python monorepo managed with `uv`. The main package is `bob-server`, a FastAPI application.

## Database

- **Engine**: SQLite via `aiosqlite` with WAL journal mode and foreign keys enabled
- **Default location**: `~/data/bob.db`
- **Override**: Set `BOB_DB_PATH` environment variable
- **Data directory**: `~/data/` (override with `BOB_DATA_DIR`)
- **Config directory**: `~/config/` (override with `BOB_CONFIG_DIR`)
- **Connection pool**: 4 connections by default (override with `BOB_DB_POOL_SIZE`)
- **Migrations**: SQL files in `packages/bob-server/bob_server/schemas/`, numbered and applied automatically on startup via `apply_migrations()`

## Server

- **Default host/port**: `127.0.0.1:8420` (override with `BOB_HOST` / `BOB_PORT`)
- **Entry point**: `packages/bob-server/bob_server/main.py`
- **Dashboard**: React SPA built into `ui_dist/` and served at `/dashboard`. Dev server runs from `packages/bob-server/bob_server/ui_app/`.
- **API**: REST endpoints under `/api/v1/` plus `/dashboard/api/*` for the SPA

## Key Directories

- `packages/bob-server/bob_server/` - Main server package
  - `routers/` - FastAPI routers (dashboard_api is itself a package split by domain)
  - `cli/` - Typer CLI split by subapp
  - `models.py` - Pydantic models
  - `config.py` - Settings (env vars, paths)
  - `database.py` - Database connection pool
  - `schemas/` - SQL migration files
  - `ui_app/` - React SPA source (Vite + TypeScript + Tailwind)
  - `services/` - Background services (email polling, whatsapp bridge, etc.)

## Runtime paths

The database is at `/home/bob/data/bob.db`
Fetch logs for the running service using `journalctl` command e.g. `journalctl --user -u bob.service --since "10 min ago"  # recent`
The workspace directory is `/home/bob/workspace`
Config directory is `/home/bob/config`

## Development

- Package manager: `uv`
- Python version: check `pyproject.toml`
