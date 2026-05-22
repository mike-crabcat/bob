# Cyborg

Cyborg is a fully autonomous AI agent that replaces OpenClaw. It reasons, plans, communicates, and acts independently across WhatsApp, email, voice chat, and phone calls. A human describes an aim and success criteria, and Cyborg plans the work into tasks, executes them, and iterates until the criteria are met. It maintains its own memory, manages contacts and calendars, and can reach out to people on your behalf through any supported channel.

## Feature Areas

### Autonomous Projects and Tasks

Cyborg's core execution engine manages projects with aims, methods, plans, and measurable success criteria. Projects break into tasks, tasks break into steps, and the system drives through them automatically. Each task follows a state machine (`planning` -> `pending` -> `active` -> `completed`/`failed`, with `paused` and `blocked` states). Tasks can be retried, escalated, or decomposed into subtasks. After each task completes, Cyborg evaluates whether success criteria are met, refines strategy if needed, and generates follow-up tasks for remaining gaps. Projects can auto-execute, progressing through plan steps without human intervention. A journal captures milestones, decisions, blockers, and results. Learning extraction produces reusable insights from completed projects.

### WhatsApp Messaging

Cyborg connects to WhatsApp through a Go companion service (the WhatsApp Bridge) that links to WhatsApp Web via the `whatsmeow` library. It handles both direct messages and group chats, with support for text, images, and documents. Messages flow through a persistent SQLite queue with guaranteed delivery and automatic retries. Contacts are auto-seeded from shared contact cards. Proactive outreach tools let Cyborg initiate conversations with trusted contacts. A pairing system supports both QR code and phone number methods.

### Voice Chat

Real-time voice conversations run over WebSockets with a full STT/TTS pipeline. Cyborg uses Faster Whisper for speech-to-text (with CUDA acceleration) and Omnivoice for text-to-speech. The system supports barge-in detection, silence detection, multi-language conversations with language tagging, and session-based conversation tracking. Voice sessions are stored as unified session messages alongside text and email interactions.

### Phone Calls

Phone integration uses Twilio Media Streams for real-time bidirectional audio during voice calls. Cyborg can initiate outbound calls to contacts and handle inbound calls, with automatic contact resolution from caller ID. Calls are recorded and each exchange is logged with detailed timing metrics (STT latency, LLM latency, TTS latency, end-to-end latency). The `make_phone_call` tool lets the LLM dial contacts directly, and `get_call_status` tracks call progress.

### Email

Cyborg reads and sends email through AgentMail. A polling service checks inboxes for new messages, resolves contacts from sender addresses, and dispatches the LLM with email context and reply tools. The system supports multiple inboxes, thread management, attachment handling (downloaded from trusted senders), and trust-based handling policies. Replies are sent back through the AgentMail API.

### Dispatch System

Every agent interaction is tracked as a dispatch, whether it is a task assignment, a WhatsApp message response, an email reply, a voice conversation, or a phone call. Dispatches have their own lifecycle (active, completed, failed, timed_out, cancelled) with concurrency limits, stuck detection, and automatic tapping. This gives Cyborg a unified view of everything the agent is doing across all channels.

### Session Management

Every conversation is a session, identified by a session key that encodes the channel and peer (e.g. `agent:main:whatsapp:direct:+61400111222`). Sessions have agendas (purpose and handling instructions), participants (with trust levels), and periodic summaries. Session routes map logical keys to physical channels for cross-session routing. The unified `session_messages` table stores all conversation history across voice, phone, email, and WhatsApp.

### Reflection

The reflection service lets users ask questions about any session's history. It builds a transcript from the session's messages and LLM call log, then dispatches an LLM call to analyze the conversation, trace tool invocations, explain agent decisions, and identify errors or missed opportunities. This is useful for debugging agent behavior and understanding what happened during an autonomous session.

### Dashboard

A React-based web dashboard provides real-time monitoring and management. It shows active sessions across all channels, LLM call statistics and latency metrics, contact management, workspace file browsing, phone call recordings with transcripts, and active dispatch monitoring. A WebSocket connection provides live updates.

### Reasoning and Planning

The project system uses LLM-powered reasoning to generate plans from aims, evaluate success criteria, refine strategy after task completions, generate follow-up tasks for gaps, produce health assessments, and extract learnings from completed projects.

### Calendars and Events

Calendars support color-coded entries with events, recurring schedules, and recipient tracking. Events link to contacts and sessions for cross-channel reminders and notifications.

### Notifications and Webhooks

Persisted notifications with acknowledgement, delivery state, and repeat throttling. Webhook delivery with retry tracking for external integrations. Cross-session routing for source and target session resolution.

## Data Model

```text
Legend: 1 --< many, >--< many-to-many, self = self-reference

=== Tasks and Projects ===

+------------------------+      +------------------------+
| tasks                  |1 --< | task_steps             |
| PK id                  |      | PK id                  |
| FK parent_id -> tasks  |      | FK task_id -> tasks.id |
| FK current_plan_id     |      +------------------------+
|    -> plans.id         |
| plan, result,          |1 --< +------------------------+
| retry_config,          +------ | task_history           |
| metadata, blocked_*,   |       | PK id                  |
| notification_*,        |       | FK task_id -> tasks.id |
| target_*, files        |       +------------------------+
+------------------------+
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
|    -> project_specs.id |      +------------------------+
| method, plan,          |
| success_criteria,      |1 --< +------------------------+
| subagent_session_key,  +------ | project_specs          |
| metadata, blocked_*,   |       | PK id, FK project_id   |
| notification_*,        |       | version_number, aim,   |
| updated_at             |       | method, plan, criteria,|
+------------------------+       | status, is_current     |
          |                      +------------------------+
          | 1 --< +------------------------+
          +------ | project_insights       |
          |       | PK id, FK project_id   |
          |       | outcome_type, category,|
          |       | insight_data           |
          |       +------------------------+
          |
          | 1 --< +------------------------+
          +------ | project_health_checks  |
                  | PK id, FK project_id   |
                  | health_score,          |
                  | risk_level, indicators |
                  +------------------------+

+------------------------+ >--< +------------------------+ >--1 +------------------------+
| projects               |      | project_tasks          |      | tasks                  |
| PK id                  |      | PK (project_id,task_id)|      | PK id                  |
+------------------------+      +------------------------+      +------------------------+

=== Sessions and Messaging ===

+------------------------+      +------------------------+
| session_routes         | >--1 | contacts               |
| PK id                  |      | PK id                  |
| session_key, channel,  |      | is_default, phone,     |
| kind, chat_id,         |      | email, whatsapp_groups,|
| FK contact_id          |      | trust_level, metadata  |
+------------------------+      +------------------------+

+------------------------+
| session_messages       |
| PK id                  |
| session_key, role,     |
| content, metadata      |
+------------------------+

+------------------------+      +------------------------+
| session_agendas        |      | session_participants   |
| PK id                  |      | PK id                  |
| session_key, purpose,  |      | session_key,           |
| handling_instructions  |      | contact_id, trust,     |
+------------------------+      | last_active            |
                                +------------------------+

+------------------------+
| session_summaries      |
| PK id                  |
| session_key, summary,  |
| topics, participants   |
+------------------------+

=== Dispatches ===

+------------------------+
| dispatches             |
| PK id                  |
| session_key, category, |
| status, task_id,       |
| tap_count, duration,   |
| metadata               |
+------------------------+

=== Phone Calls ===

+------------------------+1 --< +----------------------------+
| phone_calls            |      | phone_call_exchanges       |
| PK id                  |      | PK id                      |
| call_sid, direction,   |      | FK phone_call_id           |
| status, duration,      |      | transcript, timing metrics |
| recording_path,        |      | (STT, LLM, TTS, e2e)       |
| contact_id, agenda     |      +----------------------------+
+------------------------+

=== Email ===

+------------------------+1 --< +------------------------+
| email_inboxes          |      | email_messages          |
| PK id                  |      | PK id                   |
| inbox_id, status,      |      | FK inbox_id             |
| metadata               |      | message_id, thread_id,  |
+------------------------+      | from/to/subject/body,   |
                                | attachments (JSON)      |
                                +------------------------+

+------------------------+
| email_threads          |
| PK id                  |
| thread_id, session_key,|
| contact_id, project_id,|
| agenda                 |
+------------------------+

=== Calendars ===

+------------------------+1 --< +------------------------+1 --< +------------------------+
| calendars              |      | events                 |      | event_recipients       |
| PK id                  |      | PK id                  |      | PK id                  |
| metadata               |      | FK calendar_id         |      | FK event_id            |
+------------------------+      +------------------------+      +------------------------+

=== Other ===

+------------------------+1 --< +------------------------+
| webhook_configs        |      | webhook_deliveries     |
| PK id                  |      | PK id                  |
| events                 |      | FK webhook_id          |
+------------------------+      +------------------------+

+------------------------+
| notifications          |
| PK id                  |
| entity_type,           |
| entity_id,             |
| notification_type,     |
| status, delivery_*,    |
| metadata               |
+------------------------+

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

+------------------------+
| skill_delegations      |
| PK id                  |
| dispatch_id, skill,    |
| status, result         |
+------------------------+
```

## System Architecture

```text
                         +-----------------+
                         |   Web Dashboard |
                         |   (React SPA)   |
                         +--------+--------+
                                  |
                          HTTP/WS | :8420
                                  |
+--------+   +--------+   +------+-------+   +-----------+   +-----------+
| WhatsApp|   |  Email |   |              |   |  Voice    |   |   Phone   |
| Bridge  |   | (Agent |   |  Cyborg      |   |  Chat     |   |  (Twilio) |
| (Go)    |   |  Mail) |   |  Server      |   |  (WS/STT/ |   |  (Media   |
|         |   |        |   |  (FastAPI)   |   |   TTS)    |   |  Streams) |
+----+----+   +----+---+   +------+-------+   +-----+-----+   +-----+-----+
     |             |               |                 |               |
     | WS :8430    |  Polling      |                 | WS            | WS
     |             |               |                 |               |
+----+----+        |        +------+--------+        |               |
| WhatsApp|        |        |               |        |               |
|  Web    |        +------->|    SQLite     |<-------+               |
| (whats- |                 |   Database    |                        |
|  meow)  |                 |               |                        |
+---------+                 +---------------+                        |
                                                                     |
                               +-------------------+                 |
                               |      Twilio       |<----------------+
                               |   (PSTN/Mobile)   |
                               +-------------------+
```

## Setup

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- Go 1.22+ (for WhatsApp bridge)
- CUDA GPU (recommended for voice STT)
- A Twilio account (for phone calls)
- An AgentMail account (for email)

### Install Cyborg Server

```bash
git clone <repo-url> cyborg
cd cyborg
uv sync --extra dev
```

### Configure Environment

Cyborg reads `CYBORG_*` settings from the process environment and auto-loads `.env` files.

Load order:

1. Existing process environment
2. `CYBORG_ENV_FILE`, if set
3. `.env` in the current working directory
4. `.env` in the resolved config directory, usually `~/.config/cyborg/.env`

Create the config directory and env file:

```bash
mkdir -p ~/.config/cyborg
cat > ~/.config/cyborg/.env <<'EOF'
# Core
CYBORG_PORT=8420
CYBORG_DASHBOARD_SECRET=your-dashboard-secret

# LLM (pick one or both)
CYBORG_OPENAI_API_KEY=sk-...
CYBORG_OPENCLAW_BASE_URL=https://openclaw.example
CYBORG_OPENCLAW_TOKEN=secret

# WhatsApp Bridge
CYBORG_WHATSAPP_BRIDGE_ENABLED=true
CYBORG_WHATSAPP_BRIDGE_URL=ws://127.0.0.1:8430/ws
CYBORG_WHATSAPP_BRIDGE_TOKEN=your-bridge-token

# Email (optional)
CYBORG_AGENTMAIL_API_KEY=...

# Phone (optional)
CYBORG_PHONE_ENABLED=true
CYBORG_PHONE_TWILIO_ACCOUNT_SID=AC...
CYBORG_PHONE_TWILIO_AUTH_TOKEN=...
CYBORG_PHONE_TWILIO_PHONE_NUMBER=+1...
CYBORG_PHONE_BASE_URL=https://your-public-url
EOF
```

### Install and Start the WhatsApp Bridge

The WhatsApp bridge is a Go companion service that connects to WhatsApp Web:

```bash
cd services/whatsappbridge
make build
make install   # copies binary to ~/.local/bin/whatsappbridge
```

Configure the bridge. Create `~/.local/share/cyborg/whatsappbridge/.env` or set environment variables:

```bash
export WHATSAPPBRIDGE_HOST=127.0.0.1
export WHATSAPPBRIDGE_PORT=8430
export WHATSAPPBRIDGE_TOKEN=your-bridge-token    # must match CYBORG_WHATSAPP_BRIDGE_TOKEN
export WHATSAPPBRIDGE_DATA_DIR=$HOME/.local/share/cyborg/whatsappbridge
```

Start the bridge:

```bash
whatsappbridge
```

Then pair your WhatsApp account. Use the Cyborg CLI to request a pairing code:

```bash
uv run cyborg whatsapp pair --phone +61400111222
```

Or scan a QR code from the dashboard at `http://localhost:8420/dashboard`.

### Start Cyborg

Run directly:

```bash
uv run cyborg serve
```

Or install as a systemd user service:

```bash
uv run cyborg install
uv run cyborg start
```

The service listens on `127.0.0.1:8420` by default.

- Dashboard: `http://localhost:8420/dashboard`
- Swagger UI: `http://localhost:8420/docs`
- ReDoc: `http://localhost:8420/redoc`
- Health: `http://localhost:8420/health`

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
| `call` | `list`, `get` |
| `openai` | `chat`, `evaluate` |
| `eval` | `run`, `list`, `show` |

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
| `deprecated` | Task no longer relevant |

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
4. Once approved, start the project -- linked tasks become actionable
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
- For group targets, register a session route so Cyborg can resolve the concrete target session key.
- Standard WhatsApp DM targets do not need a registered DM session route. Cyborg derives the target session key as `agent:<agent-id>:whatsapp:direct:+<e164>` from the contact phone number.

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

Cyborg can use LLM reasoning capabilities to generate and refine project plans:

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

Compact context summaries for injecting into agent sessions:

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

## Configuration Reference

### General

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
| `CYBORG_DASHBOARD_SECRET` | *(none)* | Shared secret for dashboard operations |
| `CYBORG_PROJECTS_BASE_DIR` | `~/.openclaw/workspace/projects` | Base directory for project workspaces |
| `CYBORG_HEARTBEAT_INTERVAL_SECONDS` | `60` | Heartbeat and notification dispatch interval |

### LLM

| Variable | Default | Description |
|---|---|---|
| `CYBORG_OPENAI_API_KEY` | *(none)* | OpenAI API key |
| `CYBORG_OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI API base URL |
| `CYBORG_OPENAI_DEFAULT_MODEL` | `gpt-5.4-mini` | Default model |
| `CYBORG_OPENAI_TIMEOUT_SECONDS` | `120` | Request timeout |
| `CYBORG_OPENAI_WEB_SEARCH` | `false` | Enable web search tool |

### OpenClaw Integration

| Variable | Description |
|---|---|
| `CYBORG_OPENCLAW_BASE_URL` | OpenClaw HTTP base URL |
| `CYBORG_OPENCLAW_TOKEN` | OpenClaw API token |
| `CYBORG_OPENCLAW_GATEWAY_URL` | OpenClaw gateway websocket URL (defaults from base URL) |
| `CYBORG_OPENCLAW_GATEWAY_TOKEN` | Gateway auth token (defaults to `CYBORG_OPENCLAW_TOKEN`) |
| `CYBORG_OPENCLAW_AGENT_ID` | Agent ID for target task-assignment turns |
| `CYBORG_OPENCLAW_TIMEOUT_SECONDS` | Request timeout |

### WhatsApp Bridge

| Variable | Default | Description |
|---|---|---|
| `CYBORG_WHATSAPP_BRIDGE_ENABLED` | `false` | Enable WhatsApp bridge client |
| `CYBORG_WHATSAPP_BRIDGE_URL` | `ws://127.0.0.1:8430/ws` | Bridge WebSocket URL |
| `CYBORG_WHATSAPP_BRIDGE_TOKEN` | *(none)* | Auth token for bridge connection |
| `CYBORG_WHATSAPP_BRIDGE_RECONNECT_INTERVAL_SECONDS` | `10` | Reconnect interval |

WhatsApp bridge (Go companion) variables:

| Variable | Default | Description |
|---|---|---|
| `WHATSAPPBRIDGE_HOST` | `127.0.0.1` | Bridge listen host |
| `WHATSAPPBRIDGE_PORT` | `8430` | Bridge listen port |
| `WHATSAPPBRIDGE_TOKEN` | *(none)* | Auth token (must match Cyborg side) |
| `WHATSAPPBRIDGE_DATA_DIR` | `~/.local/share/cyborg/whatsappbridge` | Data directory |
| `WHATSAPPBRIDGE_LOG_LEVEL` | `info` | Log level |

### Voice

| Variable | Default | Description |
|---|---|---|
| `CYBORG_VOICE_ENABLED` | `true` | Enable voice chat |
| `CYBORG_VOICE_STT_MODEL` | `large-v3-turbo` | Faster Whisper model |
| `CYBORG_VOICE_STT_DEVICE` | `cuda` | STT device (cuda/cpu) |
| `CYBORG_VOICE_STT_COMPUTE_TYPE` | `int8` | STT compute type |
| `CYBORG_VOICE_TTS_NUM_STEPS` | `16` | TTS generation steps |
| `CYBORG_VOICE_VOICES_DIR` | `~/.openclaw/bobvoice-voices` | Voice profiles directory |
| `CYBORG_VOICE_SESSION_MAX_AGE_DAYS` | `30` | Session data retention |

### Phone

| Variable | Default | Description |
|---|---|---|
| `CYBORG_PHONE_ENABLED` | `false` | Enable phone integration |
| `CYBORG_PHONE_TWILIO_ACCOUNT_SID` | *(none)* | Twilio Account SID |
| `CYBORG_PHONE_TWILIO_AUTH_TOKEN` | *(none)* | Twilio Auth Token |
| `CYBORG_PHONE_TWILIO_PHONE_NUMBER` | *(none)* | Twilio phone number |
| `CYBORG_PHONE_BASE_URL` | *(none)* | Public URL for Twilio callbacks |
| `CYBORG_PHONE_CALL_RECORDING_ENABLED` | `true` | Record calls |
| `CYBORG_PHONE_CALL_RECORDING_MAX_AGE_DAYS` | `30` | Recording retention |

### Email

| Variable | Default | Description |
|---|---|---|
| `CYBORG_AGENTMAIL_API_KEY` | *(none)* | AgentMail API key |
| `CYBORG_AGENTMAIL_DEFAULT_INBOX_ID` | *(none)* | Default inbox |
| `CYBORG_AGENTMAIL_POLL_INTERVAL_SECONDS` | `30` | Inbox poll interval |
| `CYBORG_EMAIL_POLLING_ENABLED` | `true` | Enable email polling |

### Dispatch

| Variable | Default | Description |
|---|---|---|
| `CYBORG_DISPATCH_CONCURRENCY_LIMIT` | `10` | Max concurrent dispatches |
| `CYBORG_DISPATCH_STUCK_TIMEOUT_MINUTES` | `60` | Timeout before dispatch is considered stuck |
| `CYBORG_DISPATCH_SHUTDOWN_TIMEOUT_SECONDS` | `30` | Grace period on shutdown |

### Webhook Templates

| Variable | Description |
|---|---|
| `CYBORG_WEBHOOK_{NAME}_URL` | URL for webhook named `{NAME}` |
| `CYBORG_WEBHOOK_{NAME}_SECRET` | Secret for webhook named `{NAME}` |
| `CYBORG_WEBHOOK_{NAME}_EVENTS` | Comma-separated events for webhook named `{NAME}` |

### Skill Environment Variables

Skills run as subprocesses and need API keys in standard env var names (e.g. `OPENAI_API_KEY`). Since Cyborg runs as a systemd user service, it does not inherit your shell environment -- it reads `~/.config/cyborg/.env` at startup. To make an API key available to skills:

1. Add the key to `~/.config/cyborg/.env` with the `CYBORG_` prefix:

```bash
echo 'CYBORG_GOOGLE_PLACES_API_KEY=AIza...' >> ~/.config/cyborg/.env
```

2. Register the mapping in `packages/cyborg-server/cyborg_server/services/skill_env.py` so the subprocess sees the standard name:

```python
ENV_MAPPINGS: dict[str, str] = {
    "CYBORG_OPENAI_API_KEY": "OPENAI_API_KEY",
    "CYBORG_OPENAI_BASE_URL": "OPENAI_BASE_URL",
    "CYBORG_AGENTMAIL_API_KEY": "AGENTMAIL_API_KEY",
    "CYBORG_GOOGLE_PLACES_API_KEY": "GOOGLE_PLACES_API_KEY",
}
```

3. Restart the service:

```bash
uv run cyborg restart
```

Skills can then use `os.environ.get("GOOGLE_PLACES_API_KEY")` or rely on SDK auto-detection.

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
- Phone recordings: `~/.local/share/cyborg/calls/`
- WhatsApp bridge data: `~/.local/share/cyborg/whatsappbridge/`
- Service: systemd user service (`cyborg.service`)
