# Cyborg

Cyborg is Bob's SQLite-backed memory and planning service. It exposes a FastAPI HTTP API for tasks, projects, contacts, notifications, calendars, events, webhooks, and compact context summaries, plus a Typer CLI for local and systemd-managed operation.

## Why?

OpenClaw already has sessions, transcripts, memory, tools, and webhooks. Cyborg exists to add durable application state and business rules on top of that.

What Cyborg allows OpenClaw to do that it cannot do cleanly by itself:

- Keep a normalized, queryable database of tasks, projects, calendars, contacts, notifications, and relationships between them instead of relying on transcript inference.
- Enforce workflow rules such as `planning -> pending -> active`, plan approval gates, blocked-task resume instructions, and project/task linkage.
- Persist notifications until acknowledged, track delivery attempts, throttle repeats, and distinguish source-session prompts from target-session task assignment.
- Support cross-session work as first-class data: a task can originate in one session, be actioned in another, and report back to the source.
- Provide stable APIs and CLI commands for external clients, automation, dashboards, and maintenance tasks.
- Produce compact context summaries from structured state instead of rebuilding everything from raw chat history.

In short: OpenClaw is the agent runtime and conversation engine. Cyborg is the structured system-of-record for workflow, planning, routing, and notification policy.

## Features

- **Hierarchical tasks** with subtasks, retry policies, recurring schedules, steps, and audit history
- **Blocked task state** for tasks waiting on user input with full resume instructions
- **Project-task relationships** — projects can spin off tasks, and task completions auto-generate project journal entries
- **Projects** with journal entries and task links
- **Calendars, events, and recipient tracking**
- **Persisted notifications** with acknowledgement, delivery state, and repeat throttling
- **Session route registry** for resolving logical session keys into concrete outbound targets
- **Webhook delivery configuration and retry tracking**
- **Direct OpenClaw delivery** via gateway `send`, with target task assignment delivered through gateway `agent`
- **OpenClaw context export** in text and JSON forms
- **Context endpoints** tuned for Bob's constrained context window
- **Soft deletes** across primary entities
- **SQLite migrations** loaded automatically from `cyborg/schemas/`

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

## Schema

```text
Legend: 1 --< many, >--< many-to-many, self = self-reference

+------------------------+      +------------------------+
| tasks                  |1 --< | task_steps             |
| PK id                  |      | PK id                  |
| FK parent_id -> tasks  |      | FK task_id -> tasks.id |
| FK current_plan_id     |      +------------------------+
|    -> plans.id         |
| plan, retry_config,    |
| metadata, blocked_*,   |
| notification_*         |
+------------------------+
          | 1 --< +------------------------+
          +------ | task_history           |
          |       | PK id                  |
          |       | FK task_id -> tasks.id |
          |       +------------------------+
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
| method, plan,          |      | FK project_id          |
| success_criteria,      |      | metadata               |
| subagent_session_key,  |      +------------------------+
| metadata, blocked_*,   |
| notification_*         |
+------------------------+

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
| standalone table       |
+------------------------+

+------------------------+      +------------------------+
| session_routes         |      | contacts               |
| PK id                  | >--1 | PK id                  |
| session_key, channel,  |      | standalone table       |
| kind, chat_id,         |      +------------------------+
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
      |          |          |
      |          |          +---- events
      |          +--------------- projects
      +-------------------------- tasks
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

## Configuration

Cyborg reads `CYBORG_*` settings from the process environment and now auto-loads `.env` files.

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

The service listens on `127.0.0.1:8420` by default.

- Swagger UI: `http://localhost:8420/docs`
- ReDoc: `http://localhost:8420/redoc`
- Health: `http://localhost:8420/health`

## CLI

Inspect the installed command surface:

```bash
uv run cyborg --help
uv run cyborg task --help
uv run cyborg project --help
uv run cyborg contact --help
uv run cyborg notification --help
uv run cyborg session-route --help
uv run cyborg calendar --help
uv run cyborg event --help
uv run cyborg webhook --help
uv run cyborg openclaw --help
```

Service management:

```bash
uv run cyborg install
uv run cyborg start
uv run cyborg restart
uv run cyborg status
uv run cyborg logs -f
uv run cyborg stop
uv run cyborg uninstall
```

API command groups:

- `task`: `create`, `list`, `get`, `update`, `start`, `complete`, `fail`, `retry`, `block`, `unblock`, `steps`, `step-add`, `subtask-create`, `history`, `delete`
- `task plan`: `submit`, `list`, `get`, `approve`, `approve-id`, `reject`, `reject-id`
- `project`: `create`, `list`, `get`, `update`, `start`, `pause`, `close`, `tasks`, `task-create`, `journal`, `journal-add`, `execute`, `evaluate`, `delete`
- `project spec`: `submit`, `list`, `get`, `approve`, `approve-id`, `reject`, `reject-id`
- `contact`: `create`, `list`, `get`, `update`, `delete`, `by-phone`, `by-email`, `by-whatsapp-group`
- `notification`: `list`, `get`, `ack`, `process-due`
- `session-route`: `create`, `list`, `get`, `update`, `delete`
- `calendar`: `create`, `list`, `get`, `update`, `delete`
- `event`: `create`, `list`, `get`, `update`, `delete`, `confirm`, `cancel`, `recipients`, `recipient-add`, `recipient-update`
- `context`: `summary`, `tasks`, `projects`, `calendar`
- `webhook`: `create`, `list`, `get`, `by-name`, `update`, `delete`, `deliveries`, `delivery-get`, `delivery-retry`, `process-pending`
- `openclaw`: `context`

Structured payload flags:

- Use `--metadata-json`, `--details-json`, `--plan-json`, and `--success-criteria-json` when an endpoint accepts nested JSON.
- Repeat `--project-id`, `--task-id`, `--event`, and `--whatsapp-group` to supply multiple values.
- Use `--session-key`, `--channel`, and `--chat-id` on tasks, contacts, and calendars to identify the source session for approvals, reminders, and status prompts.
- Use `--target-kind`, `--target-session-key`, `--target-chat-id`, and `--target-contact-id` on tasks to identify the target session where the task should be actioned.
- `--target-kind group` routes to a WhatsApp group and should be paired with `--target-session-key` or `--target-chat-id`.
- `--target-kind dm` routes to a WhatsApp direct message and should be paired with `--target-contact-id`.
- If you use a group `session_key`, register it first with `cyborg session-route create ...`. For OpenClaw-backed task assignment, that should be the real OpenClaw session key, typically `agent:main:whatsapp:group:<group-jid>`.
- Standard WhatsApp DM targets do not need a DM session route. Cyborg derives the default target session key from the contact phone number and OpenClaw agent id.
- Use `/api/v1/notifications` or `cyborg notification ...` for user prompting. The context endpoints are snapshots, not notification feeds.

Direct development mode:

```bash
uv run cyborg serve --host 127.0.0.1 --port 8420
```

### Task and Plan CLI Examples

```bash
uv run cyborg task create "Prepare weekly review" \
  --requested-by Bob \
  --priority high \
  --plan "1. Gather inputs. 2. Draft review. 3. Send summary." \
  --retry-max-attempts 2 \
  --retry-on-failure retry_from \
  --retry-from-step 2

uv run cyborg task create "Ask the family which night works" \
  --plan "1. Ask the family group. 2. Wait for responses. 3. Summarize back to the origin session." \
  --channel whatsapp \
  --session-key agent:main:whatsapp:group:120363400000000000@g.us \
  --target-kind group \
  --target-session-key agent:main:whatsapp:group:120363426096069246@g.us

uv run cyborg task create "Ask Alice for the quote" \
  --plan "1. DM Alice. 2. Wait for response. 3. Report back." \
  --channel whatsapp \
  --session-key agent:main:whatsapp:group:120363400000000000@g.us \
  --target-kind dm \
  --target-contact-id <contact-id>

uv run cyborg task update <task-id> \
  --status blocked \
  --blocked-reason "Waiting for API key from David" \
  --blocked-resume-instructions "Add the key to .env, test /health, then resume step 3."

uv run cyborg task step-add <task-id> \
  --step-number 2 \
  --description "Export customer table" \
  --status active

uv run cyborg task steps <task-id>
uv run cyborg task history <task-id>

uv run cyborg task plan submit <task-id> --content "Revised plan: 1. Gather inputs. 2. Validate output. 3. Report result."
uv run cyborg task plan approve <task-id> --approver Mike
uv run cyborg task plan get <plan-id>
```

### Cross-Session Task Routing

Tasks can carry both a source session and a target session.

- Source session: `--channel`, `--session-key`, `--chat-id`. This is where Cyborg sends planning prompts, approval requests, reminders, and status updates.
- Target session: `--target-kind`, `--target-session-key`, `--target-chat-id`, `--target-contact-id`. This is where the task should be actioned.
- `cyborg project task-create ...` accepts the same source and target routing flags as `cyborg task create ...`.
- For DM targets, look up or create the contact first, then pass its id with `--target-contact-id`.
- For source or target routes that specify `session_key`, add a matching session route so Cyborg can resolve the concrete OpenClaw recipient.
- Standard WhatsApp DM targets do not need a registered DM session route. Cyborg derives the real OpenClaw target session key as `agent:<agent-id>:whatsapp:direct:+<e164>` from the contact phone number. Add an explicit DM session route only if you need to override that default.
- For target task assignment, group targets still need a real OpenClaw session key when you route by `target_session.session_key`. Cyborg sends the task context into that target session with a gateway `agent` run, and the agent's first reply is the visible outbound message.

Examples:

```bash
uv run cyborg session-route create agent:main:whatsapp:group:120363426096069246@g.us \
  --kind group \
  --chat-id 120363426096069246@g.us

uv run cyborg task create "Check if the family is free on Friday" \
  --plan "1. Ask the family group. 2. Collect answers. 3. Report back." \
  --channel whatsapp \
  --session-key agent:main:whatsapp:group:120363400000000000@g.us \
  --target-kind group \
  --target-session-key agent:main:whatsapp:group:120363426096069246@g.us

uv run cyborg task create "Get Alice's ETA" \
  --plan "1. DM Alice. 2. Wait for reply. 3. Report back." \
  --channel whatsapp \
  --session-key agent:main:whatsapp:group:120363400000000000@g.us \
  --target-kind dm \
  --target-contact-id <contact-id>
```

### Project CLI Examples

```bash
uv run cyborg project create "Q1 Data Migration" \
  --description "Full migration of customer records from legacy system" \
  --session-key whatsappgroup-main \
  --channel whatsapp

uv run cyborg project spec submit <project-id> \
  --aim "Migrate legacy data to new schema" \
  --method "Extract the data, transform it, load it into the new schema, and verify the output." \
  --plan-json '[{"title":"Extract","description":"Export source data","criteria":"data exported","order":0}]' \
  --success-criteria-json '[{"check":"records_migrated > 0","description":"Some records were migrated"}]'

uv run cyborg project spec approve <project-id> --approver Mike

uv run cyborg project update <project-id> --auto-execute

uv run cyborg project task-create <project-id> "Extract customer data" --priority high --plan "1. Export source data. 2. Verify row counts. 3. Save artifact."
uv run cyborg project journal-add <project-id> --type milestone --content "Completed phase 1"
uv run cyborg project execute <project-id>
uv run cyborg project evaluate <project-id>
```

Projects cannot be started or auto-executed until a project spec with `aim`, `method`, and `success_criteria` has been approved by a user.

### Contact CLI Examples

```bash
uv run cyborg contact create "Alice Example" \
  --phone-number 0400111222 \
  --email alice@example.com \
  --whatsapp-group family \
  --session-key whatsappgroup-family

uv run cyborg contact list --search Alice
uv run cyborg contact by-phone 0400111222
uv run cyborg contact by-email alice@example.com
uv run cyborg contact by-whatsapp-group family
```

### Notification CLI Examples

```bash
uv run cyborg notification list
uv run cyborg notification list --entity-type task
uv run cyborg notification get <notification-id>
uv run cyborg notification ack <notification-id> --acknowledged-by mobile-client
uv run cyborg notification process-due
```

### Session Route CLI Examples

```bash
uv run cyborg session-route create agent:main:whatsapp:group:120363426096069246@g.us \
  --kind group \
  --chat-id 120363426096069246@g.us

uv run cyborg session-route create agent:main:whatsapp:direct:+61400111222 \
  --kind dm \
  --contact-id <contact-id>

uv run cyborg session-route list
uv run cyborg session-route get <route-id>
uv run cyborg session-route update <route-id> --deactivate
```

### OpenClaw Notification Delivery

Persisted notifications are the source of outbound user prompting. Cyborg can deliver them directly to OpenClaw instead of relying on client polling.

OpenClaw-side setup is required in addition to the Cyborg env vars below.

Set these environment variables on the Cyborg service:

```bash
export CYBORG_OPENCLAW_BASE_URL="https://openclaw.example"
export CYBORG_OPENCLAW_TOKEN="secret"
export CYBORG_OPENCLAW_GATEWAY_URL="wss://openclaw.example"
# Optional if gateway auth is disabled or shares the same token.
# export CYBORG_OPENCLAW_GATEWAY_TOKEN="secret"
export CYBORG_OPENCLAW_AGENT_ID="<optional-agent-id>"
```

Notes:

- These can live in a `.env` file; Cyborg will load them automatically using the configuration search order above.
- `CYBORG_OPENCLAW_GATEWAY_URL` defaults from `CYBORG_OPENCLAW_BASE_URL` by switching `http -> ws` or `https -> wss`.
- `CYBORG_OPENCLAW_GATEWAY_TOKEN` defaults to `CYBORG_OPENCLAW_TOKEN` if unset.
- Cyborg now uses the OpenClaw gateway only. HTTP hook setup is not required for notification delivery.
- If gateway auth is enabled in OpenClaw, `CYBORG_OPENCLAW_GATEWAY_TOKEN` must match `gateway.auth.token`.
- If you set `CYBORG_OPENCLAW_AGENT_ID`, that agent is used for target task-assignment turns and for message mirroring where OpenClaw supports it.
- Recommended OpenClaw config is:

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

- `session.dmScope: "per-channel-peer"` is the important setting for WhatsApp DM task assignment. It keeps each DM on its own OpenClaw session key, for example `agent:main:whatsapp:direct:+61400111222`.
- OpenClaw must have the target channel configured and logged in. For WhatsApp, run `openclaw channels login --channel whatsapp` and make sure your `channels.whatsapp` access policy allows the DMs/groups you expect.
- If you use `cyborg session-route` for group delivery, the stored `chat_id` must be the real OpenClaw/WhatsApp target id, for example a group JID such as `120363426096069246@g.us`.
- If you use `cyborg session-route` for task assignment, any explicit route `session_key` must be the real OpenClaw session key. That is usually only needed for group targets or DM overrides, for example `agent:main:whatsapp:group:120363426096069246@g.us` or `agent:main:whatsapp:direct:+61400111222`.
- Group allowlists on the OpenClaw side still apply. If `channels.whatsapp.groups` is configured, the target group must be included there.
- Visible notification delivery goes through the OpenClaw gateway websocket `send` RPC with the resolved `channel`, `to`, and `sessionKey`.
- The background worker processes due notifications automatically while the service is running.
- `cyborg notification process-due` is available for manual dispatch and diagnostics.
- Task/project input notifications are raised immediately on state change, then daily for a limited number of repeats.
- Task assignment notifications are routed to the target session. Task result notifications and planning prompts are routed to the source session.
- For target task assignment, Cyborg uses one OpenClaw `agent` turn in the real target session. The prompt carries the hidden task context, and the assistant's first reply is the visible outbound WhatsApp/group message.
- Full setup guide: [OPENCLAW-INTEGRATION.md](/home/mike/.openclaw/workspace/projects/cyborg/OPENCLAW-INTEGRATION.md)

### Calendar and Event CLI Examples

```bash
uv run cyborg calendar create "Bob" --color "#2A9D8F" --default
uv run cyborg calendar create "Family" --session-key whatsappgroup-family --channel whatsapp
uv run cyborg calendar list

uv run cyborg event create "Standup" \
  --calendar-id <calendar-id> \
  --time 2026-03-10T09:00:00+00:00 \
  --duration 15 \
  --timezone UTC

uv run cyborg event list --calendar-id <calendar-id>
uv run cyborg event confirm <event-id>
uv run cyborg event recipient-add <event-id> --address bob@example.com --type email --name Bob
uv run cyborg event recipients <event-id>
```

### Webhook and OpenClaw CLI Examples

```bash
uv run cyborg webhook create my-webhook \
  --url https://example.com/webhook \
  --secret supersecret \
  --event task.created \
  --event task.completed

uv run cyborg webhook deliveries --status failed
uv run cyborg webhook delivery-retry <delivery-id>

uv run cyborg openclaw context --format text
uv run cyborg openclaw context --format json
```

## Task Management

### Task States

- `planning` — Task created and awaiting plan submission or plan approval
- `pending` — Plan approved and task is ready to start
- `active` — Task is in progress
- `paused` — Task temporarily paused
- `blocked` — Task waiting for user input (see [Blocked Tasks](#blocked-tasks))
- `completed` — Task finished successfully
- `failed` — Task failed (may be retryable)

### Create a Task

```bash
curl -X POST http://127.0.0.1:8420/api/v1/tasks \
  -H 'content-type: application/json' \
  -d '{
    "title": "Prepare weekly review",
    "requested_by": "Bob",
    "priority": "high",
    "plan": "1. Gather inputs. 2. Draft review. 3. Send summary.",
    "retry_config": {
      "max_attempts": 2,
      "on_failure": "retry_from",
      "retry_from_step": 2
    }
  }'
```

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

**Why this matters:** The resume instructions must be complete enough that anyone (including future Bob) can resume the task without remembering the conversation context.

To unblock:

```bash
curl -X POST http://127.0.0.1:8420/api/v1/tasks/{task_id}/unblock \
  -H 'content-type: application/json' \
  -d '{
    "notes": "David provided the key via WhatsApp"
  }'
```

### Complete a Task with Result Summary

When completing a task, you can provide a result summary that will be added to any parent projects' journals:

```bash
curl -X POST http://127.0.0.1:8420/api/v1/tasks/{task_id}/complete \
  -H 'content-type: application/json' \
  -d '{
    "result_summary": "Successfully extracted 150 records. Data saved to /data/output.csv. 3 records failed validation and were logged to errors.json."
  }'
```

## Project Management

### Create a Project

```bash
curl -X POST http://127.0.0.1:8420/api/v1/projects \
  -H 'content-type: application/json' \
  -d '{
    "title": "Q1 Data Migration",
    "aim": "Migrate legacy data to new schema",
    "description": "Full migration of customer records from legacy system"
  }'
```

### Spin Off Tasks from a Project

Projects can create tasks that are automatically linked:

```bash
curl -X POST http://127.0.0.1:8420/api/v1/projects/{project_id}/tasks \
  -H 'content-type: application/json' \
  -d '{
    "title": "Extract customer data",
    "requested_by": "Mike",
    "priority": "high",
    "plan": "Step 1: Connect to legacy DB. Step 2: Export customer table. Step 3: Validate records."
  }'
```

### Project-Task Lifecycle

When a task linked to a project is completed:
1. Task status changes to `completed`
2. A journal entry of type `result` is automatically added to the project
3. The journal entry includes the task title and any result summary provided

This creates an automatic audit trail of work completed within the project.

### Project Journal

Manually add journal entries for milestones, decisions, or blockers:

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

## Calendars and Events

### Create a Calendar

```bash
curl -X POST http://127.0.0.1:8420/api/v1/calendars \
  -H 'content-type: application/json' \
  -d '{"name": "Bob", "color": "#2A9D8F", "is_default": true}'
```

### Create an Event

```bash
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

## Context API

Fetch Bob's condensed context:

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

## Webhooks

Create a webhook configuration:

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
```

Process pending deliveries:

```bash
curl -X POST http://127.0.0.1:8420/api/v1/webhooks/process-pending
```

## OpenClaw Integration

Fetch OpenClaw-formatted context:

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

## Data Storage

- Database: `~/.local/share/cyborg/cyborg.db`
- Config: `~/.config/cyborg/`
- Service: systemd user service (`cyborg.service`)

## Environment Variables

- `CYBORG_HOST` — Bind address (default: 127.0.0.1)
- `CYBORG_PORT` — Port (default: 8420)
- `CYBORG_DATA_DIR` — Data directory
- `CYBORG_LOG_LEVEL` — Logging level
