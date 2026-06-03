# Cyborg - AI Agent Platform

## Architecture

This is a Python monorepo managed with `uv`. The main package is `cyborg-server`, a FastAPI application.

## Database

- **Engine**: SQLite via `aiosqlite` with WAL journal mode and foreign keys enabled
- **Default location**: `~/.local/share/cyborg/cyborg.db`
- **Override**: Set `CYBORG_DB_PATH` environment variable
- **Data directory**: `~/.local/share/cyborg/` (override with `CYBORG_DATA_DIR`)
- **Config directory**: `~/.config/cyborg/` (override with `CYBORG_CONFIG_DIR`)
- **Connection pool**: 4 connections by default (override with `CYBORG_DB_POOL_SIZE`)
- **Migrations**: SQL files in `packages/cyborg-server/cyborg_server/schemas/`, numbered and applied automatically on startup via `apply_migrations()`

Note: There is an empty `data/cyborg.db` at the project root — this is a leftover, not the active database.

## Server

- **Default host/port**: `127.0.0.1:8420` (override with `CYBORG_HOST` / `CYBORG_PORT`)
- **Entry point**: `packages/cyborg-server/cyborg_server/main.py`
- **Dashboard**: Served at `/`, `/emails`, `/contacts`, `/projects`, etc.
- **API**: REST endpoints under `/api/v1/`

## Key Directories

- `packages/cyborg-server/cyborg_server/` - Main server package
  - `routers/` - FastAPI routers (dashboard, contacts, etc.)
  - `models.py` - Pydantic models
  - `config.py` - Settings (env vars, paths)
  - `database.py` - Database connection pool
  - `schemas/` - SQL migration files
  - `templates/dashboard/` - Jinja2 HTML templates (cyberpunk dark theme with Tailwind CSS, HTMX)
  - `services/` - Background services (email polling, etc.)

## Runtime paths

The cyborg.db is at `/home/bob/.local/share/cyborg/cyborg.db`
Fetch logs for the running service using `journalctl` comand e.g. `journalctl --user -u cyborg.service --since "10 min ago"  # recent`
The workspace directory is `/home/bob/.config/cyborg/harness`

## Development

- Package manager: `uv`
- Python version: check `pyproject.toml`
