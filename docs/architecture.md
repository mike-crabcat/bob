# Cyborg Architecture

## System Overview

```mermaid
flowchart LR
    Bob[Bob / OpenClaw] -->|HTTP| API[FastAPI Application]
    CLI[Typer CLI] -->|systemctl or uvicorn| API
    API --> Routers[API Routers]
    Routers --> Services[Service Layer]
    Services --> DB[SQLite via aiosqlite]
    DB --> Schemas[Schema SQL Migrations]
```

## Runtime Data Flow

```mermaid
sequenceDiagram
    participant Bob as Bob
    participant API as FastAPI Router
    participant Service as Domain Service
    participant SQLite as SQLite

    Bob->>API: POST /api/v1/tasks
    API->>Service: validate request body
    Service->>SQLite: insert task + audit history
    SQLite-->>Service: persisted row
    Service-->>API: TaskResponse
    API-->>Bob: 201 Created
```

## Context Summary Flow

```mermaid
flowchart TD
    Request[GET /api/v1/context/summary] --> QueryTasks[Aggregate task status counts]
    Request --> QueryProjects[Aggregate project state counts]
    Request --> QueryEvents[Select upcoming events]
    QueryTasks --> Merge[Build condensed response]
    QueryProjects --> Merge
    QueryEvents --> Merge
    Merge --> Response[ContextSummaryResponse]
```

## API Summary

### Tasks

- `GET /api/v1/tasks`
- `POST /api/v1/tasks`
- `GET /api/v1/tasks/{id}`
- `PUT /api/v1/tasks/{id}`
- `DELETE /api/v1/tasks/{id}`
- `POST /api/v1/tasks/{id}/start`
- `POST /api/v1/tasks/{id}/complete`
- `POST /api/v1/tasks/{id}/fail`
- `POST /api/v1/tasks/{id}/retry`
- `GET /api/v1/tasks/{id}/steps`
- `POST /api/v1/tasks/{id}/steps`
- `POST /api/v1/tasks/{id}/subtasks`
- `GET /api/v1/tasks/{id}/history`

### Projects

- `GET /api/v1/projects`
- `POST /api/v1/projects`
- `GET /api/v1/projects/{id}`
- `PUT /api/v1/projects/{id}`
- `DELETE /api/v1/projects/{id}`
- `POST /api/v1/projects/{id}/start`
- `POST /api/v1/projects/{id}/pause`
- `POST /api/v1/projects/{id}/close`
- `GET /api/v1/projects/{id}/journal`
- `POST /api/v1/projects/{id}/journal`
- `GET /api/v1/projects/{id}/tasks`

### Calendars and Events

- `GET /api/v1/calendars`
- `POST /api/v1/calendars`
- `GET /api/v1/calendars/{id}`
- `PUT /api/v1/calendars/{id}`
- `DELETE /api/v1/calendars/{id}`
- `GET /api/v1/events`
- `POST /api/v1/events`
- `GET /api/v1/events/{id}`
- `PUT /api/v1/events/{id}`
- `DELETE /api/v1/events/{id}`
- `POST /api/v1/events/{id}/confirm`
- `POST /api/v1/events/{id}/cancel`
- `GET /api/v1/events/{id}/recipients`
- `POST /api/v1/events/{id}/recipients`
- `PUT /api/v1/events/{id}/recipients/{rid}`

### Context

- `GET /api/v1/context/summary`
- `GET /api/v1/context/tasks`
- `GET /api/v1/context/projects`
- `GET /api/v1/context/calendar`

## Database Schema

```mermaid
erDiagram
    TASKS ||--o{ TASK_STEPS : has
    TASKS ||--o{ TASK_HISTORY : records
    TASKS ||--o{ TASKS : parents
    PROJECTS ||--o{ PROJECT_JOURNAL_ENTRIES : records
    PROJECTS ||--o{ PROJECT_TASKS : links
    TASKS ||--o{ PROJECT_TASKS : links
    CALENDARS ||--o{ EVENTS : owns
    EVENTS ||--o{ EVENT_RECIPIENTS : notifies

    TASKS {
        text id PK
        text title
        text status
        text priority
        text parent_id FK
        text retry_config
        integer is_recurring
        text recurrence_rule
        text next_run_at
        text deleted_at
    }
    TASK_STEPS {
        text id PK
        text task_id FK
        integer step_number
        text status
    }
    TASK_HISTORY {
        text id PK
        text task_id FK
        text action
        text details
        text timestamp
    }
    PROJECTS {
        text id PK
        text title
        text state
        text conclusion
        text deleted_at
    }
    PROJECT_JOURNAL_ENTRIES {
        text id PK
        text project_id FK
        text entry_type
        text content
        text metadata
    }
    PROJECT_TASKS {
        text project_id FK
        text task_id FK
    }
    CALENDARS {
        text id PK
        text name
        integer is_default
        text deleted_at
    }
    EVENTS {
        text id PK
        text calendar_id FK
        text title
        text start_time
        text end_time
        text status
        text deleted_at
    }
    EVENT_RECIPIENTS {
        text id PK
        text event_id FK
        text recipient_type
        text recipient_address
        text status
    }
```

## Storage Notes

- Migrations are plain SQL files in `cyborg/schemas/`, tracked in `schema_migrations`.
- Soft deletes are implemented with `deleted_at` on primary entities.
- Task history is append-only and records lifecycle and mutation events.
- The database layer uses a small async queue-backed connection pool plus a write lock to keep SQLite writes serialized.
