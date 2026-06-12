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
- **Dashboard**: Served at `/`, `/emails`, `/contacts`, `/projects`, etc.
- **API**: REST endpoints under `/api/v1/`

## Key Directories

- `packages/bob-server/bob_server/` - Main server package
  - `routers/` - FastAPI routers (dashboard, contacts, etc.)
  - `models.py` - Pydantic models
  - `config.py` - Settings (env vars, paths)
  - `database.py` - Database connection pool
  - `schemas/` - SQL migration files
  - `templates/dashboard/` - Jinja2 HTML templates (cyberpunk dark theme with Tailwind CSS, HTMX)
  - `services/` - Background services (email polling, etc.)

## Runtime paths

The database is at `/home/bob/data/bob.db`
Fetch logs for the running service using `journalctl` command e.g. `journalctl --user -u bob.service --since "10 min ago"  # recent`
The workspace directory is `/home/bob/workspace`
Config directory is `/home/bob/config`

## Development

- Package manager: `uv`
- Python version: check `pyproject.toml`
