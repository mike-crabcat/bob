# Cyborg Design Document

## Objective

Build a SQLite-backed HTTP data service that provides Bob (OpenClaw bot) with CRUD storage for tasks, projects, and calendars.

## Core Schemas

### 1. Tasks Schema

Tasks support hierarchical structure, retry loops, and long-running periodic tasks.

```
tasks
├── id: UUID (PK)
├── title: str
├── description: str
├── requested_by: str          # Who asked for the task
├── plan: str                  # Bob's intended plan
├── status: enum               # pending, active, paused, completed, failed
├── priority: enum             # low, medium, high, critical
├── parent_id: UUID (FK)       # For subtasks
├── retry_config: JSON         # Retry loop configuration
│   ├── max_attempts: int
│   ├── current_attempt: int
│   ├── on_failure: str        # action: retry, escalate, abort
│   └── fallback_subtask_id: UUID
├── is_recurring: bool
├── recurrence_rule: str       # Cron expression for periodic tasks
├── next_run_at: datetime      # For recurring tasks
├── created_at: datetime
├── updated_at: datetime
├── started_at: datetime
├── completed_at: datetime
└── metadata: JSON             # Flexible extra data

task_steps                      # Track progress through plan steps
├── id: UUID (PK)
├── task_id: UUID (FK)
├── step_number: int
├── description: str
├── status: enum               # pending, active, completed, failed
├── result: str
├── started_at: datetime
└── completed_at: datetime

task_history                    # Audit log of task changes
├── id: UUID (PK)
├── task_id: UUID (FK)
├── action: str                # created, started, step_completed, failed, etc.
├── details: JSON
└── timestamp: datetime
```

**Key Features:**
- Subtasks can reference parent tasks
- Retry loops: if a subtask fails, can return to first subtask for alternative approach
- Recurring tasks for daily/weekly long-term work
- Step-by-step progress tracking

### 2. Projects Schema

```
projects
├── id: UUID (PK)
├── title: str
├── description: str
├── aim: str                   # Project objective
├── state: enum                # planning, active, paused, closed
├── created_at: datetime
├── started_at: datetime
├── paused_at: datetime
├── closed_at: datetime
└── conclusion: str            # Summary when closed

project_journal_entries
├── id: UUID (PK)
├── project_id: UUID (FK)
├── entry_type: enum           # note, milestone, decision, blocker, result
├── content: str
├── created_at: datetime
└── metadata: JSON

project_tasks                   # Link tasks to projects
├── project_id: UUID (FK)
└── task_id: UUID (FK)
```

### 3. Calendars Schema

```
calendars
├── id: UUID (PK)
├── name: str
├── description: str
├── color: str                 # For UI display
├── is_default: bool
└── created_at: datetime

events
├── id: UUID (PK)
├── calendar_id: UUID (FK)
├── title: str
├── description: str
├── agenda: str                # Meeting agenda
├── venue: str
├── start_time: datetime
├── end_time: datetime
├── timezone: str
├── is_all_day: bool
├── recurrence_rule: str       # RRULE format
├── status: enum               # tentative, confirmed, cancelled
├── created_at: datetime
└── updated_at: datetime

event_recipients
├── id: UUID (PK)
├── event_id: UUID (FK)
├── recipient_type: enum       # email, phone, channel
├── recipient_address: str
├── name: str
├── status: enum               # pending, confirmed, declined, tentative
├── responded_at: datetime
└── notes: str
```

## API Design

### RESTful Endpoints

```
# Tasks
GET    /api/v1/tasks                    # List tasks (with filters)
POST   /api/v1/tasks                    # Create task
GET    /api/v1/tasks/{id}               # Get task details
PUT    /api/v1/tasks/{id}               # Update task
DELETE /api/v1/tasks/{id}               # Delete task
POST   /api/v1/tasks/{id}/start         # Start task
POST   /api/v1/tasks/{id}/complete      # Complete task
POST   /api/v1/tasks/{id}/fail          # Mark as failed
POST   /api/v1/tasks/{id}/retry         # Trigger retry
GET    /api/v1/tasks/{id}/steps         # Get task steps
POST   /api/v1/tasks/{id}/steps         # Add/update steps
POST   /api/v1/tasks/{id}/subtasks      # Create subtask
GET    /api/v1/tasks/{id}/history       # Get task history

# Projects
GET    /api/v1/projects                 # List projects
POST   /api/v1/projects                 # Create project
GET    /api/v1/projects/{id}            # Get project
PUT    /api/v1/projects/{id}            # Update project
DELETE /api/v1/projects/{id}            # Delete project
POST   /api/v1/projects/{id}/start      # Start project
POST   /api/v1/projects/{id}/pause      # Pause project
POST   /api/v1/projects/{id}/close      # Close project
GET    /api/v1/projects/{id}/journal    # Get journal entries
POST   /api/v1/projects/{id}/journal    # Add journal entry
GET    /api/v1/projects/{id}/tasks      # Get linked tasks

# Calendars
GET    /api/v1/calendars                # List calendars
POST   /api/v1/calendars                # Create calendar
GET    /api/v1/calendars/{id}           # Get calendar
PUT    /api/v1/calendars/{id}           # Update calendar
DELETE /api/v1/calendars/{id}           # Delete calendar

# Events
GET    /api/v1/events                   # List events (with date range)
POST   /api/v1/events                   # Create event
GET    /api/v1/events/{id}              # Get event
PUT    /api/v1/events/{id}              # Update event
DELETE /api/v1/events/{id}              # Delete event
POST   /api/v1/events/{id}/confirm      # Confirm event
POST   /api/v1/events/{id}/cancel       # Cancel event
GET    /api/v1/events/{id}/recipients   # List recipients
POST   /api/v1/events/{id}/recipients   # Add recipient
PUT    /api/v1/events/{id}/recipients/{rid}  # Update recipient status

# Context (for Bob's context window)
GET    /api/v1/context/summary          # Get summary of all data
GET    /api/v1/context/tasks            # Task summary for context
GET    /api/v1/context/projects         # Project summary for context
GET    /api/v1/context/calendar         # Calendar summary for context
```

## CLI Tool

The CLI provides service lifecycle management:

```bash
# Service management
cyborg install      # Create systemd user service
                    # - Creates ~/.config/systemd/user/cyborg.service
                    # - Creates ~/.local/share/cyborg/ for data
                    # - Enables service

cyborg uninstall    # Remove systemd service and data

cyborg start        # Start the service (systemctl --user start)
cyborg stop         # Stop the service
cyborg restart      # Restart the service
cyborg status       # Check if running + health
cyborg logs         # Show logs (journalctl --user -u cyborg)
cyborg logs -f      # Follow logs

# Direct run (for development)
cyborg serve        # Run server directly in foreground
```

## Architecture Diagrams

### System Context

```
┌─────────────────────────────────────────────────────────────┐
│                      OpenClaw Gateway                        │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐     │
│  │   Bob Bot   │    │   Cron Jobs │    │   Hooks     │     │
│  └──────┬──────┘    └──────┬──────┘    └──────┬──────┘     │
│         │                  │                  │            │
│         └──────────────────┼──────────────────┘            │
│                            │                               │
│                     ┌──────┴──────┐                        │
│                     │  cyborg CLI │                        │
│                     └──────┬──────┘                        │
└────────────────────────────┼────────────────────────────────┘
                             │ HTTP
┌────────────────────────────┼────────────────────────────────┐
│                     ┌──────┴──────┐                        │
│                     │   FastAPI   │                        │
│                     │   Service   │                        │
│                     └──────┬──────┘                        │
│                            │                               │
│                     ┌──────┴──────┐                        │
│                     │    SQLite   │                        │
│                     │   (~/local) │                        │
│                     └─────────────┘                        │
│                                                            │
│              Cyborg Data Service (localhost:8420)          │
└─────────────────────────────────────────────────────────────┘
```

### Data Flow - Task Creation

```
Bob receives request
       │
       ▼
┌──────────────┐
│  cyborg CLI  │  cyborg task create --title "..."
└──────┬───────┘
       │ HTTP POST /api/v1/tasks
       ▼
┌──────────────┐
│   FastAPI    │  Validate (Pydantic)
│   Router     │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│   Service    │  Business logic
│   Layer      │  - Generate UUID
│              │  - Set defaults
│              │  - Create steps if plan provided
└──────┬───────┘
       │
       ▼
┌──────────────┐
│   SQLite     │  INSERT INTO tasks
│   Database   │  INSERT INTO task_steps (if plan)
└──────────────┘
       │
       ▼
   Return 201
   with task JSON
```

### Retry Loop Flow

```
Task has subtasks A → B → C
       │
       ▼
   Subtask B fails
       │
       ▼
┌──────────────┐    Yes    ┌──────────────┐
│ Retry config │──────────►│ Return to A  │
│ on_failure=  │           │ (alternative)│
│ "retry_from" │           └──────┬───────┘
└──────┬───────┘                  │
       │ No                       │
       ▼                          │
┌──────────────┐                  │
│ Escalate to  │◄─────────────────┘
│ parent or    │
│ mark failed  │
└──────────────┘
```

## Implementation Notes

### Database Design
- Use UUIDs for all primary keys (stored as TEXT in SQLite)
- JSON columns for flexible metadata
- Foreign keys with ON DELETE CASCADE where appropriate
- Indexes on commonly queried fields (status, state, start_time)

### API Design
- Consistent response envelope: `{ "data": ..., "error": null, "meta": {} }`
- Pagination for list endpoints (cursor-based)
- Filtering via query params: `?status=active&priority=high`
- Soft deletes (set deleted_at) rather than hard deletes

### Context Summary Endpoints
These provide condensed views for Bob's context window:
- `/context/tasks` - Active tasks with progress
- `/context/projects` - Active projects with recent journal entries
- `/context/calendar` - Upcoming events (next 7 days)

### Error Handling
- HTTP status codes: 200, 201, 400, 404, 409, 500
- Structured error responses with error codes
- Validation errors include field-level details

## Future Extensions

- WebSocket support for real-time updates
- Full-text search for tasks/projects
- Attachments (stored as files, referenced in DB)
- Multi-user support with permissions
- Export/import (JSON/CSV)
