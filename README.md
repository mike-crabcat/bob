# Cyborg

Cyborg autonomously executes projects. A human describes an aim and success criteria, Cyborg plans the work into tasks, and the system iterates until the criteria are met. OpenClaw is the brain — it reasons, plans, evaluates, and decides. Cyborg is the body — it stores state, enforces workflows, tracks progress, and drives the execution loop.

## How it works

1. **Define an aim.** You create a project with a goal, a method, and measurable success criteria.
2. **Generate a plan.** OpenClaw breaks the aim into ordered plan steps, each with its own completion criteria.
3. **Execute autonomously.** Cyborg creates tasks for each plan step, drives them through to completion, and moves to the next step automatically.
4. **Evaluate and adapt.** After each task, OpenClaw evaluates whether success criteria are met, refines strategy if needed, and generates follow-up tasks for anything still outstanding.
5. **Finish.** When all criteria are satisfied, the project closes. OpenClaw extracts learnings for future projects.

You intervene only at approval points or when a task blocks for your input. Otherwise the loop runs on its own.

## Cyborg and OpenClaw: the split

**OpenClaw** is an agent runtime. It has sessions, transcripts, memory, tools, and webhooks. It can reason and act within a conversation, but it has no durable project state and no workflow engine.

**Cyborg** is the structured system-of-record and execution engine that sits alongside OpenClaw. It provides what OpenClaw alone cannot:

| Responsibility | Owned by |
|---|---|
| Reasoning, planning, evaluation, learning | OpenClaw |
| Storing projects, tasks, plans, journal entries | Cyborg |
| Driving the execution loop (create task → complete → next step) | Cyborg |
| Enforcing state machines and approval gates | Cyborg |
| Building context for reasoning prompts | Cyborg |
| Routing notifications and task assignments across sessions | Cyborg |
| Deciding *what* to do next and *whether* criteria are met | OpenClaw |
| Tracking dependencies and auto-releasing blocked tasks | Cyborg |
| Persisting insights and learnings from completed work | Cyborg |

In short: OpenClaw decides, Cyborg remembers and drives.

## Reasoning types

All reasoning is performed by OpenClaw. Cyborg builds the context, calls the reasoning service, and acts on the result. There are seven reasoning types:

### Plan generation

Generates a structured execution plan from a project aim and method. Returns an ordered list of plan steps, each with a title, description, and completion criteria. Used when a new project is created or when a plan needs to be regenerated from scratch.

### Criteria evaluation

Semantically evaluates whether a project's success criteria have been met. Takes the full project context — all tasks, journal entries, outputs — and reasons about whether each criterion is satisfied. Returns which criteria are met, which are unmet, and the reasoning behind each judgment. Falls back to rule-based evaluation (numeric comparisons) if OpenClaw is unavailable.

### Strategy refinement

Analyzes a project's progress after each task completion. Determines whether the current approach is working or needs adjustment. Can suggest reprioritizing tasks, adding new tasks, changing the method, or flagging risks. Refinements are auto-applied — new tasks are created and priorities updated without human approval.

### Follow-up generation

When a project's success criteria are not all met, this generates concrete follow-up tasks to address the gaps. Each suggested task includes a title, description, execution plan, and priority. Takes the list of unmet criteria and the current project context to produce targeted next actions.

### Task planning

Generates an execution plan for a single task. Considers the project context, the task's dependencies and output files, and produces a concise set of steps the agent should follow. Used when a task is started and needs a concrete action plan.

### Health analysis

Assesses the overall health of a project. Considers task completion rates, blockers, timeline, and any risks or issues. Returns a health score (0–1), risk level (low/medium/high/critical), identified blockers, and recommendations. Used for monitoring and to flag projects that need attention.

### Learning extraction

Extracts insights from a completed project. Identifies patterns in planning, execution, estimation, communication, technical approach, and coordination. Returns categorized insights with applicability patterns and impact assessments. These learnings are stored and reused when planning future projects with similar aims.

## Context building

Each reasoning call needs project context, and different reasoning types need different amounts. The context builder produces four scopes:

| Scope | Size | Includes |
|---|---|---|
| Minimal | 1–2k tokens | Current state, recent milestones and blockers |
| Standard | 5–10k tokens | Recent activity, key items, important journal entries |
| Comprehensive | 20–30k tokens | Everything relevant, summarized where needed |
| Full | 30k+ tokens | All context (rare, for deep analysis) |

The builder assembles project metadata, objectives, plan summary, success criteria, filtered tasks with output files, journal narrative, temporal context, and dependency information into a structured prompt for each reasoning type.

## Architecture

Cyborg is a FastAPI service backed by SQLite, with a Typer CLI for local and systemd-managed operation.

```text
cyborg/
├── cyborg/
│   ├── cli.py                # Typer CLI
│   ├── config.py             # Configuration management
│   ├── database.py           # Database connection and migrations
│   ├── main.py               # FastAPI application
│   ├── models.py             # Pydantic models
│   ├── exceptions.py         # Custom exceptions
│   ├── dependencies.py       # FastAPI dependency injection
│   ├── structured_logging.py # Correlation-aware logging
│   ├── routers/              # API route handlers
│   ├── services/             # Business logic
│   └── schemas/              # SQLite migrations
├── openclaw-plugin/          # OpenClaw context injection plugin
├── assets/                   # Static assets
├── tests/
├── docs/
└── pyproject.toml
```

## Features

### Task Management

- Hierarchical tasks with subtasks, retry policies, recurring schedules, steps, and audit history
- State machine: `planning` → `pending` → `active` → `completed` / `failed`, with `paused` and `blocked` states
- Blocked task state with full resume instructions for lossless context recovery
- Versioned plans with approval workflow (draft → pending_approval → approved / rejected)
- Retry policies: retry, retry_from_step, escalate, or abort on failure

### Project Management

- Projects with aims, methods, plans, and success criteria
- Project specs with versioned approval workflow — projects cannot start until a spec is approved
- Auto-executing projects that progress through plan steps autonomously
- Dependency-driven task release — completing a task automatically unblocks dependent tasks
- Strategy refinement after task completion for continuous improvement
- Journal entries (note, milestone, decision, blocker, result) with automatic result entries on task completion

### Health Monitoring

- Project health scores (0–1) with risk levels (low, medium, high, critical)
- Automated health scans with anomaly detection
- Recommendations for projects needing attention

### Learning

- Insight extraction from completed projects (planning, execution, estimation, communication, technical, coordination)
- Similar project discovery for reuse
- Success criteria suggestions based on historical patterns

### Notifications and Routing

- Persisted notifications with acknowledgement, delivery state, and repeat throttling
- Cross-session routing — source session for planning prompts, target session for task assignment
- Direct OpenClaw delivery via gateway websocket
- Default contact fallback for unroutable notifications

### Other

- Calendars, events, and recipient tracking
- Webhook delivery with retry tracking
- Session route registry for resolving logical session keys
- Context API producing compact summaries for constrained context windows
- Web dashboard for project management, approvals, and log viewing
- OpenClaw context plugin for automatic context injection
- Structured logging with correlation IDs and database-backed log storage
- Prompt history tracking for audit and analysis
- Soft deletes across primary entities
- SQLite migrations loaded automatically from `cyborg/schemas/`

## Schema

```text
Legend: 1 --< many, >--< many-to-many, self = self-reference

+------------------------+      +------------------------+
| tasks                  |1 --< | task_steps             |
| PK id                  |      | PK id                  |
| FK parent_id -> tasks  |      | FK task_id -> tasks.id |
| FK current_plan_id     |      +------------------------+
|    -> plans.id         |
| plan, result,          |
| retry_config,          |1 --< +------------------------+
| metadata, blocked_*,   +------ | task_history           |
| notification_*,        |       | PK id                  |
| target_*               |       | FK task_id -> tasks.id |
+------------------------+       +------------------------+
          |
          | 1 --< +------------------------+
          +------ | plans                  |
          |       | PK id                  |
          |       | FK task_id -> tasks.id |
          |       | content, status        |
          |       +------------------------+
          |
          ' self via parent_id

+------------------------+      +------------------------+
| projects               |1 --< | project_journal_entries|
| PK id                  |      | PK id                  |
| FK current_spec_id     |      | FK project_id          |
|    -> project_specs.id |      | metadata               |
| method, plan,          |      +------------------------+
| success_criteria,      |
| subagent_session_key,  |
| metadata, blocked_*,   |
| notification_*,        |
| updated_at             |
+------------------------+
          | 1 --< +------------------------+
          +------ | project_specs          |
          |       | PK id                  |
          |       | FK project_id          |
          |       | version_number, aim,   |
          |       | method, plan,          |
          |       | success_criteria,      |
          |       | status, is_current     |
          |       +------------------------+
          |
          | 1 --< +------------------------+
          +------ | project_insights       |
          |       | PK id                  |
          |       | FK project_id          |
          |       | outcome_type,          |
          |       | insight_category,      |
          |       | insight_data,          |
          |       | applicability_pattern  |
          |       +------------------------+
          |
          | 1 --< +------------------------+
          +------ | project_health_checks  |
          |       | PK id                  |
          |       | FK project_id          |
          |       | check_type,            |
          |       | health_score,          |
          |       | risk_level, indicators |
          |       +------------------------+

+------------------------+ >--< +------------------------+ >--1 +------------------------+
| projects               |      | project_tasks          |      | tasks                  |
| PK id                  |      | PK (project_id,task_id)|      | PK id                  |
+------------------------+      | FK project_id          |      +------------------------+
                                | FK task_id             |
                                +------------------------+

+------------------------+1 --< +------------------------+1 --< +------------------------+
| calendars              |      | events                 |      | event_recipients       |
| PK id                  |      | PK id                  |      | PK id                  |
| metadata               |      | FK calendar_id         |      | FK event_id            |
+------------------------+      +------------------------+      +------------------------+

+------------------------+1 --< +------------------------+
| webhook_configs        |      | webhook_deliveries     |
| PK id                  |      | PK id                  |
| events                 |      | FK webhook_id          |
+------------------------+      +------------------------+

+------------------------+
| contacts               |
| PK id                  |
| is_default,            |
| phone_number, email,   |
| whatsapp_groups,       |
| metadata               |
+------------------------+

+------------------------+      +------------------------+
| session_routes         |      | contacts               |
| PK id                  | >--1 | PK id                  |
| session_key, channel,  |      +------------------------+
| kind, chat_id,         |
| FK contact_id          |
+------------------------+

+------------------------+
| notifications          |
| PK id                  |
| entity_type,           |
| entity_id,             |
| notification_type,     |
| status, delivery_*,    |
| metadata               |
+------------------------+
      ^          ^          ^
      |          |          |
      |          |          +---- events
      |          +--------------- projects
      +-------------------------- tasks

+------------------------+
| approvals              |
| PK id                  |
| approval_type,         |
| entity_id, title,      |
| proposal_data, status, |
| priority               |
+------------------------+

+------------------------+
| structured_logs        |
| PK id (auto)           |
| level, logger, message,|
| event_type,            |
| correlation_id,        |
| project_id, extra_data |
+------------------------+

+------------------------+
| prompt_history         |
| PK id                  |
| category, prompt_text, |
| project_id, task_id,   |
| session_key,           |
| token_count_estimate   |
+------------------------+
```

Most rich fields such as `metadata`, task/project `plan`, `success_criteria`, webhook `events`, delivery `payload`, and notification metadata are stored as JSON in `TEXT` columns.

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
- Dashboard: `http://localhost:8420/dashboard`

## Configuration

Cyborg reads `CYBORG_*` settings from the process environment and auto-loads `.env` files.

Load order:

1. Existing process environment
2. `CYBORG_ENV_FILE`, if set
3. `.env` in the current working directory
4. `.env` in the resolved config directory, usually `~/.config/cyborg/.env`

Examples:

```bash
cat > .env <<'EOF'
CYBORG_PORT=8420
CYBORG_OPENCLAW_BASE_URL=https://openclaw.example
CYBORG_OPENCLAW_TOKEN=secret
EOF

uv run cyborg serve
```

Or point at a specific file:

```bash
export CYBORG_ENV_FILE=~/.config/cyborg/production.env
uv run cyborg serve
```

### Environment Variables

**General:**

| Variable | Default | Description |
|---|---|---|
| `CYBORG_HOST` | `127.0.0.1` | Bind address |
| `CYBORG_PORT` | `8420` | Port |
| `CYBORG_DATA_DIR` | `~/.local/share/cyborg` | Data directory |
| `CYBORG_CONFIG_DIR` | `~/.config/cyborg` | Config directory |
| `CYBORG_DB_PATH` | `{data_dir}/cyborg.db` | Database path |
| `CYBORG_LOG_LEVEL` | `info` | Logging level |
| `CYBORG_LOG_PATH` | *(none)* | Log file path |
| `CYBORG_DB_POOL_SIZE` | `4` | Connection pool size |
| `CYBORG_PUBLIC_URL` | *(none)* | Public URL for webhook callbacks |
| `CYBORG_NOTIFICATION_DISPATCH_INTERVAL_SECONDS` | `60` | Notification dispatch interval |

**OpenClaw integration:**

| Variable | Description |
|---|---|
| `CYBORG_OPENCLAW_BASE_URL` | OpenClaw HTTP base URL |
| `CYBORG_OPENCLAW_TOKEN` | OpenClaw API token |
| `CYBORG_OPENCLAW_GATEWAY_URL` | OpenClaw gateway websocket URL (defaults from base URL) |
| `CYBORG_OPENCLAW_GATEWAY_TOKEN` | Gateway auth token (defaults to `CYBORG_OPENCLAW_TOKEN`) |
| `CYBORG_OPENCLAW_AGENT_ID` | Agent ID for target task-assignment turns |
| `CYBORG_OPENCLAW_SENDER_NAME` | Sender name for outbound messages |
| `CYBORG_OPENCLAW_WAKE_MODE` | Wake mode for gateway sessions |
| `CYBORG_OPENCLAW_TIMEOUT_SECONDS` | Request timeout |

**Webhook templates:**

| Variable | Description |
|---|---|
| `CYBORG_WEBHOOK_{NAME}_URL` | URL for webhook named `{NAME}` |
| `CYBORG_WEBHOOK_{NAME}_SECRET` | Secret for webhook named `{NAME}` |
| `CYBORG_WEBHOOK_{NAME}_EVENTS` | Comma-separated events for webhook named `{NAME}` |

## CLI

```bash
uv run cyborg --help
```

Service management:

```bash
uv run cyborg install      # Create systemd user service
uv run cyborg start        # Start service
uv run cyborg restart      # Restart service
uv run cyborg status       # Check service status
uv run cyborg logs -f      # Follow service logs
uv run cyborg stop         # Stop service
uv run cyborg uninstall    # Remove systemd service
```

### Command Reference

| Group | Commands |
|---|---|
| `task` | `create`, `list`, `get`, `update`, `start`, `complete`, `fail`, `retry`, `block`, `unblock`, `steps`, `step-add`, `subtask-create`, `history`, `delete` |
| `task plan` | `submit`, `list`, `get`, `approve`, `approve-id`, `reject`, `reject-id` |
| `project` | `create`, `list`, `get`, `update`, `start`, `pause`, `close`, `tasks`, `task-create`, `journal`, `journal-add`, `execute`, `evaluate`, `delete` |
| `project spec` | `submit`, `list`, `get`, `approve`, `approve-id`, `reject`, `reject-id` |
| `planning` | `generate`, `refine` |
| `health` | `scan`, `analyze`, `projects-needing-attention`, `latest` |
| `learning` | `extract-insights`, `similar-projects`, `active-insights`, `suggest-criteria` |
| `contact` | `create`, `list`, `get`, `update`, `delete`, `by-phone`, `by-email`, `by-whatsapp-group`, `set-default`, `get-default`, `clear-default` |
| `notification` | `list`, `get`, `ack`, `process-due` |
| `session-route` | `create`, `list`, `get`, `update`, `delete` |
| `calendar` | `create`, `list`, `get`, `update`, `delete` |
| `event` | `create`, `list`, `get`, `update`, `delete`, `confirm`, `cancel`, `recipients`, `recipient-add`, `recipient-update` |
| `context` | `summary`, `tasks`, `projects`, `calendar` |
| `webhook` | `create`, `list`, `get`, `by-name`, `update`, `delete`, `deliveries`, `delivery-get`, `delivery-retry`, `process-pending` |
| `openclaw` | `context` |

### Structured Payload Flags

- Use `--metadata-json`, `--details-json`, `--plan-json`, and `--success-criteria-json` when an endpoint accepts nested JSON.
- Repeat `--project-id`, `--task-id`, `--event`, and `--whatsapp-group` to supply multiple values.
- Use `--session-key`, `--channel`, and `--chat-id` on tasks, contacts, and calendars to identify the source session.
- Use `--target-kind`, `--target-session-key`, `--target-chat-id`, and `--target-contact-id` on tasks to identify the target session where the task should be actioned.
- `--target-kind group` routes to a WhatsApp group and should be paired with `--target-session-key` or `--target-chat-id`.
- `--target-kind dm` routes to a WhatsApp direct message and should be paired with `--target-contact-id`.

## Task States

| State | Description |
|---|---|
| `planning` | Task created and awaiting plan submission or plan approval |
| `pending` | Plan approved and task is ready to start |
| `active` | Task is in progress |
| `paused` | Task temporarily paused |
| `blocked` | Task waiting for user input (with resume instructions) |
| `completed` | Task finished successfully |
| `failed` | Task failed (may be retryable depending on retry policy) |

### Blocked Tasks

When a task needs user input to proceed, put it in `blocked` state with full context:

```bash
curl -X POST http://127.0.0.1:8420/api/v1/tasks/{task_id}/block \
  -H 'content-type: application/json' \
  -d '{
    "reason": "Waiting for API key from David",
    "resume_instructions": "When unblocked: 1) Add the API key to .env file as FIRECRAWL_API_KEY. 2) Test the connection with curl to /health endpoint. 3) Update task status to active and continue with step 3 (scrape operation)."
  }'
```

Resume instructions must be complete enough that anyone (including a future agent session) can resume the task without remembering the conversation context.

To unblock:

```bash
curl -X POST http://127.0.0.1:8420/api/v1/tasks/{task_id}/unblock \
  -H 'content-type: application/json' \
  -d '{"notes": "David provided the key via WhatsApp"}'
```

### Complete a Task with Result Summary

When completing a task, provide a result summary that will be added to any parent projects' journals:

```bash
curl -X POST http://127.0.0.1:8420/api/v1/tasks/{task_id}/complete \
  -H 'content-type: application/json' \
  -d '{
    "result_summary": "Successfully extracted 150 records. Data saved to /data/output.csv. 3 records failed validation and were logged to errors.json."
  }'
```

## Project Lifecycle

1. Create a project with a title and description
2. Submit a project spec with `aim`, `method`, `plan`, and `success_criteria`
3. The spec must be approved before the project can be started
4. Once approved, start the project — linked tasks become actionable
5. Tasks can be created manually or generated by AI planning
6. Completing a task automatically: unblocks dependent tasks, generates journal entries, and may trigger strategy refinement
7. Auto-executing projects progress through plan steps autonomously
8. Success criteria are evaluated to determine project completion

```bash
curl -X POST http://127.0.0.1:8420/api/v1/projects \
  -H 'content-type: application/json' \
  -d '{
    "title": "Q1 Data Migration",
    "aim": "Migrate legacy data to new schema",
    "description": "Full migration of customer records from legacy system"
  }'

curl -X POST http://127.0.0.1:8420/api/v1/projects/{project_id}/specs \
  -H 'content-type: application/json' \
  -d '{
    "aim": "Migrate legacy data to new schema",
    "method": "Extract the data, transform it, load it into the new schema, and verify the output.",
    "plan": [{"title":"Extract","description":"Export source data","criteria":"data exported","order":0}],
    "success_criteria": [{"check":"records_migrated > 0","description":"Some records were migrated"}]
  }'

curl -X POST http://127.0.0.1:8420/api/v1/projects/{project_id}/specs/{spec_id}/approve \
  -H 'content-type: application/json' \
  -d '{"approver": "Mike"}'

curl -X POST http://127.0.0.1:8420/api/v1/projects/{project_id}/start
```

### Project Journal

```bash
curl -X POST http://127.0.0.1:8420/api/v1/projects/{project_id}/journal \
  -H 'content-type: application/json' \
  -d '{
    "entry_type": "milestone",
    "content": "Completed phase 1: Database schema designed and approved",
    "metadata": {"phase": 1, "reviewer": "David"}
  }'
```

Entry types: `note`, `milestone`, `decision`, `blocker`, `result`

## Cross-Session Task Routing

Tasks can carry both a source session and a target session.

- **Source session**: `--channel`, `--session-key`, `--chat-id`. This is where Cyborg sends planning prompts, approval requests, reminders, and status updates.
- **Target session**: `--target-kind`, `--target-session-key`, `--target-chat-id`, `--target-contact-id`. This is where the task should be actioned.
- For DM targets, look up or create the contact first, then pass its id with `--target-contact-id`.
- For group targets, register a session route so Cyborg can resolve the concrete OpenClaw recipient.
- Standard WhatsApp DM targets do not need a registered DM session route. Cyborg derives the real OpenClaw target session key as `agent:<agent-id>:whatsapp:direct:+<e164>` from the contact phone number.

```bash
# Register a group session route
uv run cyborg session-route create agent:main:whatsapp:group:120363426096069246@g.us \
  --kind group \
  --chat-id 120363426096069246@g.us

# Route a task to a group
uv run cyborg task create "Check if the family is free on Friday" \
  --plan "1. Ask the family group. 2. Collect answers. 3. Report back." \
  --channel whatsapp \
  --session-key agent:main:whatsapp:group:120363400000000000@g.us \
  --target-kind group \
  --target-session-key agent:main:whatsapp:group:120363426096069246@g.us

# Route a task to a DM
uv run cyborg task create "Get Alice's ETA" \
  --plan "1. DM Alice. 2. Wait for reply. 3. Report back." \
  --channel whatsapp \
  --session-key agent:main:whatsapp:group:120363400000000000@g.us \
  --target-kind dm \
  --target-contact-id <contact-id>
```

## AI-Powered Planning

Cyborg can use OpenClaw's reasoning capabilities to generate and refine project plans:

```bash
# Generate a plan from a project aim
uv run cyborg planning generate \
  --aim "Migrate legacy data to new schema" \
  --method "Extract, transform, load" \
  --context-scope standard \
  --project-id <project-id>

# Refine a strategy based on recent outcomes
uv run cyborg planning refine \
  --project-id <project-id>
```

## Health Monitoring

```bash
# Scan all active projects
uv run cyborg health scan

# Analyze a specific project
uv run cyborg health analyze --project-id <project-id>

# List projects needing attention
uv run cyborg health projects-needing-attention

# Show latest health for a project
uv run cyborg health latest --project-id <project-id>
```

## Learning

```bash
# Extract insights from a completed project
uv run cyborg learning extract-insights --project-id <project-id>

# Find similar past projects
uv run cyborg learning similar-projects --aim "Migrate legacy data"

# List active insights
uv run cyborg learning active-insights

# Get success criteria suggestions
uv run cyborg learning suggest-criteria --aim "Build authentication system"
```

## Context API

Compact context summaries for injecting into OpenClaw sessions:

```bash
# Full summary
curl http://127.0.0.1:8420/api/v1/context/summary

# Tasks only
curl http://127.0.0.1:8420/api/v1/context/tasks

# Projects only
curl http://127.0.0.1:8420/api/v1/context/projects

# Calendar only
curl http://127.0.0.1:8420/api/v1/context/calendar
```

## Calendars and Events

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

## Webhooks

```bash
curl -X POST http://127.0.0.1:8420/api/v1/webhooks \
  -H 'content-type: application/json' \
  -d '{
    "name": "my-webhook",
    "url": "https://example.com/webhook",
    "secret": "supersecret",
    "events": ["task.created", "task.completed"],
    "retry_count": 3
  }'

curl -X POST http://127.0.0.1:8420/api/v1/webhooks/process-pending
```

## OpenClaw Integration

### Notification Delivery

Cyborg delivers notifications directly to OpenClaw via the gateway websocket. Set these environment variables on the Cyborg service:

```bash
CYBORG_OPENCLAW_BASE_URL="https://openclaw.example"
CYBORG_OPENCLAW_TOKEN="secret"
CYBORG_OPENCLAW_GATEWAY_URL="wss://openclaw.example"   # defaults from base URL
CYBORG_OPENCLAW_GATEWAY_TOKEN="secret"                   # defaults to CYBORG_OPENCLAW_TOKEN
CYBORG_OPENCLAW_AGENT_ID="<optional-agent-id>"
```

- Visible notification delivery goes through the OpenClaw gateway websocket `send` RPC with the resolved `channel`, `to`, and `sessionKey`.
- For target task assignment, Cyborg uses one OpenClaw `agent` turn in the real target session. The prompt carries the hidden task context, and the assistant's first reply is the visible outbound message.
- The background worker processes due notifications automatically while the service is running.
- Task/project input notifications are raised immediately on state change, then daily for a limited number of repeats.

Recommended OpenClaw config:

```json5
{
  gateway: {
    auth: {
      token: "shared-secret"
    }
  },
  session: {
    dmScope: "per-channel-peer"
  }
}
```

`session.dmScope: "per-channel-peer"` is the important setting for WhatsApp DM task assignment. It keeps each DM on its own OpenClaw session key, for example `agent:main:whatsapp:direct:+61400111222`.

### Context Plugin

For automatic context injection into every OpenClaw session, use the OpenClaw Context Plugin:

```bash
cp -r ~/.openclaw/workspace/projects/cyborg/openclaw-plugin ~/.openclaw/extensions/cyborg-context
systemctl --user restart openclaw-gateway.service
```

Optional plugin configuration in `~/.config/openclaw/openclaw.json5`:

```json5
{
  plugins: {
    cyborgContext: {
      enabled: true,
      cyborgUrl: "http://127.0.0.1:8420",
      includeProjects: true,
      includeTasks: true,
      includeEvents: true,
      cacheTtlSeconds: 300,
      maxTokens: 2000
    }
  }
}
```

### Manual Context API

```bash
# Plain text
curl http://127.0.0.1:8420/openclaw/context.txt

# JSON
curl http://127.0.0.1:8420/openclaw/context.json
```

## Testing

```bash
uv run pytest
```

The default suite is Cyborg-side and does not require a live OpenClaw model or channel transport.

### OpenClaw Live Acceptance Tests

These exercise a real OpenClaw gateway/model against Cyborg's reasoning prompts and synthetic task-assignment sessions.

Required environment:

- `OPENCLAW_ACCEPTANCE=1` or `--openclaw-live`
- `OPENCLAW_ACCEPTANCE_GATEWAY_URL`
- `OPENCLAW_ACCEPTANCE_GATEWAY_TOKEN`
- optional: `OPENCLAW_ACCEPTANCE_AGENT_ID`

Fallback environment variables: `CYBORG_OPENCLAW_GATEWAY_URL`, `CYBORG_OPENCLAW_TOKEN`, `CYBORG_OPENCLAW_AGENT_ID`

```bash
uv run pytest tests/openclaw_acceptance -m openclaw_live --openclaw-live -q
```

Failures write artifacts under `.pytest_cache/openclaw_acceptance/` for prompt, gateway, and history debugging.

## Data Storage

- Database: `~/.local/share/cyborg/cyborg.db`
- Config: `~/.config/cyborg/`
- Service: systemd user service (`cyborg.service`)
