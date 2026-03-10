# Cyborg Data Service - Build Task

Build a complete SQLite-backed HTTP data service for Bob (OpenClaw bot).

## Project Location
/home/mike/.openclaw/workspace/projects/cyborg

## Tech Stack
- Python 3.12+ with `uv` for package management
- FastAPI for HTTP API
- Pydantic v2 for data validation
- SQLite for persistence
- Typer for CLI tool

## Requirements

### 1. Database Schema (SQLite)

Create these tables in `cyborg/schemas/`:

**tasks table:**
- id (UUID TEXT PK), title, description, requested_by, plan, status (pending/active/paused/completed/failed), priority (low/medium/high/critical)
- parent_id (UUID FK for subtasks), retry_config (JSON), is_recurring, recurrence_rule, next_run_at
- created_at, updated_at, started_at, completed_at, metadata (JSON)

**task_steps table:**
- id (UUID PK), task_id (FK), step_number, description, status, result, started_at, completed_at

**task_history table:**
- id (UUID PK), task_id (FK), action, details (JSON), timestamp

**projects table:**
- id (UUID PK), title, description, aim, state (planning/active/paused/closed)
- created_at, started_at, paused_at, closed_at, conclusion

**project_journal_entries table:**
- id (UUID PK), project_id (FK), entry_type (note/milestone/decision/blocker/result), content, created_at, metadata (JSON)

**project_tasks table:**
- project_id (FK), task_id (FK)

**calendars table:**
- id (UUID PK), name, description, color, is_default, created_at

**events table:**
- id (UUID PK), calendar_id (FK), title, description, agenda, venue
- start_time, end_time, timezone, is_all_day, recurrence_rule, status (tentative/confirmed/cancelled)
- created_at, updated_at

**event_recipients table:**
- id (UUID PK), event_id (FK), recipient_type (email/phone/channel), recipient_address, name
- status (pending/confirmed/declined/tentative), responded_at, notes

### 2. Pydantic Models (cyborg/models.py)

Create complete Pydantic models for all entities with:
- Base models with common fields
- Create/Update/Response variants
- Proper validation and defaults
- JSON serialization support

### 3. FastAPI Application (cyborg/)

**main.py:** FastAPI app with:
- Lifespan context for DB initialization
- Include all routers
- Exception handlers
- Health check endpoint

**database.py:**
- SQLite connection management with aiosqlite
- Migration system (apply SQL files from schemas/)
- Connection pooling

**routers/tasks.py:** All task endpoints
**routers/projects.py:** All project endpoints  
**routers/calendars.py:** All calendar endpoints
**routers/context.py:** Context summary endpoints for Bob

**services/task_service.py:** Business logic for tasks
**services/project_service.py:** Business logic for projects
**services/calendar_service.py:** Business logic for calendars

### 4. CLI Tool (cyborg/cli.py)

Typer-based CLI with commands:
- `cyborg install` - Create systemd user service
- `cyborg uninstall` - Remove systemd service
- `cyborg start` - Start service via systemctl
- `cyborg stop` - Stop service
- `cyborg restart` - Restart service
- `cyborg status` - Check service status
- `cyborg logs` - View logs (with -f for follow)
- `cyborg serve` - Run server directly (for dev)

Service defaults:
- Port: 8420
- Data dir: ~/.local/share/cyborg/
- Config dir: ~/.config/cyborg/
- Log via journalctl (systemd) or stdout (direct)

### 5. Documentation

Create docs/architecture.md with:
- System architecture diagram (Mermaid)
- Data flow diagrams
- API endpoint summary
- Database schema diagram

### 6. Configuration

**pyproject.toml:**
- Project metadata
- Dependencies: fastapi, uvicorn, pydantic, typer, aiosqlite, python-jose
- Entry point: cyborg = cyborg.cli:app
- Development dependencies: pytest, httpx

## API Endpoints Required

### Tasks
- GET/POST /api/v1/tasks
- GET/PUT/DELETE /api/v1/tasks/{id}
- POST /api/v1/tasks/{id}/start, /complete, /fail, /retry
- GET/POST /api/v1/tasks/{id}/steps
- POST /api/v1/tasks/{id}/subtasks
- GET /api/v1/tasks/{id}/history

### Projects
- GET/POST /api/v1/projects
- GET/PUT/DELETE /api/v1/projects/{id}
- POST /api/v1/projects/{id}/start, /pause, /close
- GET/POST /api/v1/projects/{id}/journal
- GET /api/v1/projects/{id}/tasks

### Calendars
- GET/POST /api/v1/calendars
- GET/PUT/DELETE /api/v1/calendars/{id}

### Events
- GET/POST /api/v1/events
- GET/PUT/DELETE /api/v1/events/{id}
- POST /api/v1/events/{id}/confirm, /cancel
- GET/POST /api/v1/events/{id}/recipients
- PUT /api/v1/events/{id}/recipients/{rid}

### Context (for Bob)
- GET /api/v1/context/summary
- GET /api/v1/context/tasks
- GET /api/v1/context/projects
- GET /api/v1/context/calendar

## Key Features to Implement

1. **Task retry loops:** If a subtask fails and retry_config.on_failure="retry_from", return to specified step
2. **Recurring tasks:** Support cron expressions for periodic tasks
3. **Context endpoints:** Provide condensed summaries for Bob's context window
4. **Full CRUD:** Complete create, read, update, delete for all entities
5. **Soft deletes:** Use deleted_at timestamp instead of hard deletes
6. **Audit logging:** Track all task changes in task_history

## Deliverables

1. Complete Python package in /home/mike/.openclaw/workspace/projects/cyborg/
2. All source files with type hints and docstrings
3. SQL schema files
4. Working CLI tool
5. Architecture documentation with diagrams
6. README with usage examples
7. Tests for core functionality

Run `uv sync` to install dependencies after creating pyproject.toml.
Test with `cyborg serve` and verify with curl or browser at http://localhost:8420/docs

When finished, the service should be installable via `cyborg install` and manageable via the CLI.
