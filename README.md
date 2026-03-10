# Cyborg

Cyborg is Bob's SQLite-backed memory and planning service. It exposes a FastAPI HTTP API for tasks, projects, calendars, events, and compact context summaries, plus a Typer CLI for local and systemd-managed operation.

## Features

- Hierarchical tasks with subtasks, retry policies, recurring schedules, steps, and audit history
- Projects with journal entries and task links
- Calendars, events, and recipient tracking
- Context endpoints tuned for Bob's constrained context window
- Soft deletes across primary entities
- SQLite migrations loaded automatically from `cyborg/schemas/`

## Layout

```text
cyborg/
├── cyborg/
│   ├── cli.py
│   ├── config.py
│   ├── database.py
│   ├── main.py
│   ├── models.py
│   ├── routers/
│   ├── schemas/
│   └── services/
├── docs/
│   └── architecture.md
├── tests/
└── pyproject.toml
```

## Install

```bash
uv sync --extra dev
```

## Run

```bash
uv run cyborg serve
```

The service listens on `127.0.0.1:8420` by default.

- Swagger UI: `http://localhost:8420/docs`
- ReDoc: `http://localhost:8420/redoc`
- Health: `http://localhost:8420/health`

## CLI

```bash
uv run cyborg install
uv run cyborg start
uv run cyborg status
uv run cyborg logs -f
uv run cyborg stop
uv run cyborg uninstall
```

Direct development mode:

```bash
uv run cyborg serve --host 127.0.0.1 --port 8420
```

## Example API Usage

Create a task:

```bash
curl -X POST http://127.0.0.1:8420/api/v1/tasks \
  -H 'content-type: application/json' \
  -d '{
    "title": "Prepare weekly review",
    "requested_by": "Bob",
    "priority": "high",
    "retry_config": {
      "max_attempts": 2,
      "on_failure": "retry_from",
      "retry_from_step": 2
    }
  }'
```

Create a calendar and event:

```bash
curl -X POST http://127.0.0.1:8420/api/v1/calendars \
  -H 'content-type: application/json' \
  -d '{"name": "Bob", "color": "#2A9D8F", "is_default": true}'

curl -X POST http://127.0.0.1:8420/api/v1/events \
  -H 'content-type: application/json' \
  -d '{
    "calendar_id": "<calendar-id>",
    "title": "Standup",
    "start_time": "2026-03-10T09:00:00+00:00",
    "end_time": "2026-03-10T09:15:00+00:00",
    "timezone": "UTC"
  }'
```

Fetch Bob's condensed context:

```bash
curl http://127.0.0.1:8420/api/v1/context/summary
```

## Testing

```bash
uv run pytest
```
