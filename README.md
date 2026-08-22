# bob

![bob](assets/bob-hero.png)

**An opinionated personal agent with SQL-backed memory.**

bob is a small, stubborn agent backed by a SQL database.

He remembers things, runs routines, keeps workspace state, and acts through tools. He does not offer a personality selector, model picker, plugin marketplace, or twelve competing abstractions for memory.

There is no agent builder here.

There is just bob.

You too can have him.

## Feature Areas

### WhatsApp Messaging

Bob connects to WhatsApp through a Go companion service (the WhatsApp Bridge) that links to WhatsApp Web via the `whatsmeow` library. It handles both direct messages and group chats, with support for text, images, and documents. Messages flow through a persistent SQLite queue with guaranteed delivery and automatic retries. Contacts are auto-seeded from shared contact cards. Proactive outreach tools let Bob initiate conversations with trusted contacts. A pairing system supports both QR code and phone number methods.

### Voice

Realtime voice runs on the OpenAI Realtime API through one bridge (`realtime_bridge.py`) with pluggable audio sources: Twilio Media Streams for phone calls and raw browser PCM for voice-link sessions. The browser harness is free and instant, so prompts, voices, and tools can be iterated there before spending money on real calls. Calls get turn-ordered transcripts (persisted per turn), structured `report_success`/`report_failure` outcomes, time-aligned stereo recordings, barge-in handling, and callee-speaks-first phone etiquette. A legacy local STT/TTS pipeline (Faster Whisper + Omnivoice) still serves the push-to-talk language-practice frontend.

### Phone Calls

Phone integration uses Twilio Media Streams bridged to the OpenAI Realtime API for real-time bidirectional audio. Bob can place outbound calls to contacts (the LLM dispatches them via `create_subagent(agent_type="openai_voice", modality="phone")`) and handle inbound calls with automatic contact resolution from caller ID. When the agent's task is done it calls `end_call`, which terminates both the Realtime session and the Twilio leg. Browser voice-link calls (modality `voice_link`) run the same bridge without the telco leg and appear in the calls UI alongside phone calls. `get_call_status` tracks call progress.

### Email

Bob reads and sends email through AgentMail. A polling service checks inboxes for new messages, resolves contacts from sender addresses, and dispatches the LLM with email context and reply tools. The system supports multiple inboxes, thread management, attachment handling (downloaded from trusted senders), and trust-based handling policies. Replies are sent back through the AgentMail API.

### Dispatch System

Every agent interaction is tracked as a dispatch, whether it is a WhatsApp message response, an email reply, a voice conversation, or a phone call. Dispatches have their own lifecycle (active, completed, failed, timed_out, cancelled) with concurrency limits, stuck detection, and automatic tapping. This gives Bob a unified view of everything the agent is doing across all channels.

### Session Management

Every conversation is a session, identified by a session key that encodes the channel and peer (e.g. `agent:main:whatsapp:direct:+61400111222`). Sessions have agendas (purpose and handling instructions), participants (with trust levels), and periodic summaries. Session routes map logical keys to physical channels for cross-session routing. The unified `session_messages` table stores all conversation history across voice, phone, email, and WhatsApp.

### Reflection

The reflection service lets users ask questions about any session's history. It builds a transcript from the session's messages and LLM call log, then dispatches an LLM call to analyze the conversation, trace tool invocations, explain agent decisions, and identify errors or missed opportunities. This is useful for debugging agent behavior and understanding what happened during an autonomous session.

### Dashboard

A React-based web dashboard provides real-time monitoring and management. It shows active sessions across all channels, LLM call statistics and latency metrics, contact management, workspace file browsing, phone call recordings with transcripts, and active dispatch monitoring. A WebSocket connection provides live updates.

### Calendars and Events

Calendars support color-coded entries with events, recurring schedules, and recipient tracking. Events link to contacts and sessions for cross-channel reminders and notifications.

### Notifications and Webhooks

Persisted notifications with acknowledgement, delivery state, and repeat throttling. Webhook delivery with retry tracking for external integrations. Cross-session routing for source and target session resolution.

## System Architecture

```text
                              +-----------------+
                              |  Web Dashboard  |
                              |  (React SPA)    |
                              +--------+--------+
                                       |
                               HTTP/WS | :8420
                                       |
+----------+   +--------+   +----------+------------------+   +----------------+
| WhatsApp |   | Email  |   |           Bob Server        |   |    Browser     |
|  Bridge  |   | (Agent |   |           (FastAPI)         |   | voice sessions |
|   (Go)   |   |  Mail) |   |                             |   |  + realtime    |
|          |   |        |   |   Realtime Voice Bridge     |   |  test harness  |
|          |   |        |   |   (realtime_bridge.py)      |   |                |
+----+-----+   +----+---+   +------------+----------------+   +--------+-------+
     |              |                    |       |                     |
     | WS :8430     | Polling            | WSS   | WS (μ-law 8k)      | WS
     |              |                    |       |                     | (PCM 24k)
     |              |                    v       v                     |
     |              |             +-----------+ +----------------+     |
     |              |             |   OpenAI  | |    Twilio      |<----+
     |              |             | Realtime  | | Media Streams  |
     |              |             |    API    | |  (PSTN/Mobile) |
     |              |             +-----------+ +----------------+
     |              |
     |              +------------------------>+---------------+
     |                                       |    SQLite     |
     +-------------------------------------->|   Database    |
                                             +---------------+
```

All realtime voice — Twilio phone calls and browser voice-link sessions — flows through the same bridge to the OpenAI Realtime API. Dispatch, instruction building, and call placement live in `voice_dispatch_service.py`; call records (both modalities) land in the `phone_calls` table and surface in the dashboard's calls UI with live transcripts.

## Setup

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- Go 1.22+ (for WhatsApp bridge)
- CUDA GPU (only for the legacy local voice STT pipeline; realtime voice needs none)
- A Twilio account (for phone calls)
- An AgentMail account (for email)

### Install Bob Server

```bash
git clone <repo-url> bob
cd bob
uv sync --extra dev
```

### Configure Environment

Bob reads `BOB_*` settings from the process environment and auto-loads `.env` files.

Load order:

1. Existing process environment
2. `BOB_ENV_FILE`, if set
3. `.env` in the current working directory
4. `.env` in the resolved config directory, usually `~/config/.env`

Create the config directory and env file:

```bash
mkdir -p ~/config
cat > ~/config/.env <<'EOF'
# Core
BOB_PORT=8420
# API token: leave unset — a secret is auto-generated to ~/data/api_secret.
# (Setting it here would leak it to the agent's bash tool via the environment;
# see "API authentication" below.)
# BOB_DASHBOARD_SECRET=

# LLM
BOB_OPENAI_API_KEY=sk-...

# WhatsApp Bridge
BOB_WHATSAPP_BRIDGE_ENABLED=true
BOB_WHATSAPP_BRIDGE_URL=ws://127.0.0.1:8430/ws
BOB_WHATSAPP_BRIDGE_TOKEN=your-bridge-token

# Email (optional)
BOB_AGENTMAIL_API_KEY=...

# Phone (optional)
BOB_PHONE_ENABLED=true
BOB_PHONE_TWILIO_ACCOUNT_SID=AC...
BOB_PHONE_TWILIO_AUTH_TOKEN=...
BOB_PHONE_TWILIO_PHONE_NUMBER=+1...
BOB_PHONE_BASE_URL=https://your-public-url
EOF
```

### Install and Start the WhatsApp Bridge

The WhatsApp bridge is a Go companion service that connects to WhatsApp Web:

```bash
cd services/whatsappbridge
make build
make install   # copies binary to ~/.local/bin/whatsappbridge
```

Configure the bridge. Create `~/data/whatsappbridge/.env` (the bridge loads `.env` files at startup — same precedence as the Python service: existing env > `$BOB_ENV_FILE` > `./.env` > `$BOB_CONFIG_DIR/.env` > `$WHATSAPPBRIDGE_DATA_DIR/.env`):

```bash
WHATSAPPBRIDGE_HOST=127.0.0.1
WHATSAPPBRIDGE_PORT=8430
WHATSAPPBRIDGE_TOKEN=your-bridge-token    # must match BOB_WHATSAPP_BRIDGE_TOKEN
WHATSAPPBRIDGE_DATA_DIR=$HOME/data/whatsappbridge
```

Start the bridge:

```bash
whatsappbridge
```

Then pair your WhatsApp account. Use the Bob CLI to request a pairing code:

```bash
uv run bob whatsapp pair --phone +61400111222
```

Or scan a QR code from the dashboard at `http://localhost:8420/dashboard`.

### Start Bob

Run directly:

```bash
uv run bob serve
```

Or install as a systemd user service:

```bash
uv run bob install
uv run bob start
```

The service listens on `127.0.0.1:8420` by default.

- Dashboard: `http://localhost:8420/dashboard`
- Swagger UI: `http://localhost:8420/docs`
- ReDoc: `http://localhost:8420/redoc`
- Health: `http://localhost:8420/health`

### Dashboard Development

The dashboard is a React SPA (Vite + TypeScript + Tailwind) in `packages/bob-server/bob_server/ui_app/`.

```bash
cd packages/bob-server/bob_server/ui_app
npm install
npm run dev
```

The Vite dev server runs on port 5173 and proxies API and WebSocket requests to the backend at `127.0.0.1:8420`:

| Path | Proxies to |
|---|---|
| `/dashboard/api` | `http://127.0.0.1:8420` |
| `/dashboard/ws` | `ws://127.0.0.1:8420` |
| `/phone` | `http://127.0.0.1:8420` |

So the backend must be running separately (`uv run bob serve`) for the dev dashboard to work.

For production, `npm run build` outputs static files to `ui_dist/`, which the FastAPI server serves at `/dashboard`.

## API Authentication

All state-changing HTTP requests (POST/PUT/PATCH/DELETE) require the API token. GET/HEAD/OPTIONS are open, as are the endpoints called by external parties: `POST /phone/twiml`, `POST /phone/status` (Twilio callbacks), and `POST /voice/log` (public voice pages). WebSocket endpoints keep their own auth.

The token is `BOB_DASHBOARD_SECRET` if set, otherwise a secret auto-generated to `~/data/api_secret` (0600, stable across restarts). Present it via any of:

- `Authorization: Bearer <token>` header
- `X-Dashboard-Secret: <token>` header (what the CLI sends)
- `bob_dashboard_secret=<token>` cookie — easiest to set by visiting `http://<host>:8420/dashboard/api/auth?secret=<token>` once in the browser you use for the dashboard (sets the cookie for a year; works on phones). The DevTools equivalent: `document.cookie = "bob_dashboard_secret=<token>; path=/; max-age=31536000"`
- `?secret=<token>` query parameter (legacy dashboard SPA transport)

The CLI reads the token automatically. To disable the gate entirely (break-glass), set `BOB_API_AUTH_DISABLED=true` and restart.

## CLI

```bash
uv run bob --help
```

Service management:

```bash
uv run bob install      # Create systemd user service
uv run bob start        # Start service
uv run bob restart      # Restart service
uv run bob status       # Check service status
uv run bob logs -f      # Follow service logs
uv run bob stop         # Stop service
uv run bob uninstall    # Remove systemd service
```

### Command Reference

| Group | Commands |
|---|---|
| `contact` | `create`, `list`, `get`, `update`, `delete`, `by-phone`, `by-email`, `by-whatsapp-group`, `set-default`, `get-default`, `clear-default` |
| `notification` | `list`, `get`, `ack`, `process-due` |
| `session-route` | `create`, `list`, `get`, `update`, `delete` |
| `calendar` | `create`, `list`, `get`, `update`, `delete` |
| `event` | `create`, `list`, `get`, `update`, `delete`, `confirm`, `cancel`, `recipients`, `recipient-add`, `recipient-update` |
| `context` | `summary`, `calendar` |
| `webhook` | `create`, `list`, `get`, `by-name`, `update`, `delete`, `deliveries`, `delivery-get`, `delivery-retry`, `process-pending` |
| `call` | `list`, `get` |
| `email` | `inbox-list`, `inbox-get`, `send`, `reply`, `process-pending` |
| `whatsapp` | `status`, `pair`, `send`, `bridge-status` |
| `memory` | `seed` |
| `openai` | `prompt` |
| `eval` | `list`, `run`, `history` |

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
    "events": ["dispatch.created", "dispatch.completed"],
    "retry_count": 3
  }'

curl -X POST http://127.0.0.1:8420/api/v1/webhooks/process-pending
```

## Configuration Reference

### General

| Variable | Default | Description |
|---|---|---|
| `BOB_HOST` | `127.0.0.1` | Bind address |
| `BOB_PORT` | `8420` | Port |
| `BOB_DATA_DIR` | `~/data` | Data directory |
| `BOB_CONFIG_DIR` | `~/config` | Config directory |
| `BOB_DB_PATH` | `{data_dir}/bob.db` | Database path |
| `BOB_LOG_LEVEL` | `info` | Logging level |
| `BOB_LOG_PATH` | *(none)* | Log file path |
| `BOB_DB_POOL_SIZE` | `4` | Connection pool size |
| `BOB_PUBLIC_URL` | *(none)* | Public URL for webhook callbacks |
| `BOB_DASHBOARD_SECRET` | *(auto-generated)* | Explicit API token. If unset, a token is generated to `{data_dir}/api_secret` (0600). **Do not set this in `.env`** while the agent bash tool inherits the server environment — the agent could read it via `printenv`; use the generated file instead |
| `BOB_API_AUTH_DISABLED` | `false` | Kill switch: when `true`, the API token gate is bypassed entirely |
| `BOB_HEARTBEAT_INTERVAL_SECONDS` | `60` | Heartbeat and notification dispatch interval |

### LLM

| Variable | Default | Description |
|---|---|---|
| `BOB_OPENAI_API_KEY` | *(none)* | OpenAI API key |
| `BOB_OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI API base URL |
| `BOB_OPENAI_DEFAULT_MODEL` | `gpt-5.4-mini` | Default model |
| `BOB_OPENAI_TIMEOUT_SECONDS` | `120` | Request timeout |
| `BOB_OPENAI_WEB_SEARCH` | `false` | Enable web search tool |

### WhatsApp Bridge

| Variable | Default | Description |
|---|---|---|
| `BOB_WHATSAPP_BRIDGE_ENABLED` | `false` | Enable WhatsApp bridge client |
| `BOB_WHATSAPP_BRIDGE_URL` | `ws://127.0.0.1:8430/ws` | Bridge WebSocket URL |
| `BOB_WHATSAPP_BRIDGE_TOKEN` | *(none)* | Auth token for bridge connection |
| `BOB_WHATSAPP_BRIDGE_RECONNECT_INTERVAL_SECONDS` | `10` | Reconnect interval |

WhatsApp bridge (Go companion) variables:

| Variable | Default | Description |
|---|---|---|
| `WHATSAPPBRIDGE_HOST` | `127.0.0.1` | Bridge listen host |
| `WHATSAPPBRIDGE_PORT` | `8430` | Bridge listen port |
| `WHATSAPPBRIDGE_TOKEN` | *(none)* | Auth token (must match Bob side) |
| `WHATSAPPBRIDGE_DATA_DIR` | `~/data/whatsappbridge` | Data directory |
| `WHATSAPPBRIDGE_LOG_LEVEL` | `info` | Log level |

### Voice

| Variable | Default | Description |
|---|---|---|
| `BOB_VOICE_ENABLED` | `true` | Enable voice chat |
| `BOB_VOICE_STT_MODEL` | `large-v3-turbo` | Faster Whisper model |
| `BOB_VOICE_STT_DEVICE` | `cuda` | STT device (cuda/cpu) |
| `BOB_VOICE_STT_COMPUTE_TYPE` | `int8` | STT compute type |
| `BOB_VOICE_TTS_NUM_STEPS` | `16` | TTS generation steps |
| `BOB_VOICE_VOICES_DIR` | `~/.openclaw/bobvoice-voices` | Voice profiles directory |
| `BOB_VOICE_SESSION_MAX_AGE_DAYS` | `30` | Session data retention |

### Phone

| Variable | Default | Description |
|---|---|---|
| `BOB_PHONE_ENABLED` | `false` | Enable phone integration |
| `BOB_PHONE_TWILIO_ACCOUNT_SID` | *(none)* | Twilio Account SID |
| `BOB_PHONE_TWILIO_AUTH_TOKEN` | *(none)* | Twilio Auth Token |
| `BOB_PHONE_TWILIO_PHONE_NUMBER` | *(none)* | Twilio phone number |
| `BOB_PHONE_BASE_URL` | *(none)* | Public URL for Twilio callbacks |
| `BOB_PHONE_CALL_RECORDING_ENABLED` | `true` | Record calls |
| `BOB_PHONE_CALL_RECORDING_MAX_AGE_DAYS` | `30` | Recording retention |

### OpenAI Realtime (voice calls)

| Variable | Default | Description |
|---|---|---|
| `BOB_OPENAI_REALTIME_MODEL` | `gpt-realtime-2.1` | Realtime model |
| `BOB_OPENAI_REALTIME_VOICE` | `cedar` | Voice for realtime calls |
| `BOB_OPENAI_REALTIME_MAX_DURATION` | `300` | Max call duration (seconds) |
| `BOB_OPENAI_REALTIME_TURN_DETECTION` | `server_vad` | Turn detection mode |

### Email

| Variable | Default | Description |
|---|---|---|
| `BOB_AGENTMAIL_API_KEY` | *(none)* | AgentMail API key |
| `BOB_AGENTMAIL_DEFAULT_INBOX_ID` | *(none)* | Default inbox |
| `BOB_AGENTMAIL_POLL_INTERVAL_SECONDS` | `30` | Inbox poll interval |
| `BOB_EMAIL_POLLING_ENABLED` | `true` | Enable email polling |

### Dispatch

| Variable | Default | Description |
|---|---|---|
| `BOB_DISPATCH_CONCURRENCY_LIMIT` | `10` | Max concurrent dispatches |
| `BOB_DISPATCH_STUCK_TIMEOUT_MINUTES` | `60` | Timeout before dispatch is considered stuck |
| `BOB_DISPATCH_SHUTDOWN_TIMEOUT_SECONDS` | `30` | Grace period on shutdown |

### Webhook Templates

| Variable | Description |
|---|---|
| `BOB_WEBHOOK_{NAME}_URL` | URL for webhook named `{NAME}` |
| `BOB_WEBHOOK_{NAME}_SECRET` | Secret for webhook named `{NAME}` |
| `BOB_WEBHOOK_{NAME}_EVENTS` | Comma-separated events for webhook named `{NAME}` |

### Skill Environment Variables

Skills run as subprocesses and need API keys in standard env var names (e.g. `OPENAI_API_KEY`). Since Bob runs as a systemd user service, it does not inherit your shell environment -- it reads `~/config/.env` at startup. To make an API key available to skills:

1. Add the key to `~/config/.env` with the `BOB_` prefix:

```bash
echo 'BOB_GOOGLE_PLACES_API_KEY=AIza...' >> ~/config/.env
```

2. Register the mapping in `packages/bob-server/bob_server/services/skill_env.py` so the subprocess sees the standard name:

```python
ENV_MAPPINGS: dict[str, str] = {
    "BOB_OPENAI_API_KEY": "OPENAI_API_KEY",
    "BOB_OPENAI_BASE_URL": "OPENAI_BASE_URL",
    "BOB_AGENTMAIL_API_KEY": "AGENTMAIL_API_KEY",
    "BOB_GOOGLE_PLACES_API_KEY": "GOOGLE_PLACES_API_KEY",
}
```

3. Restart the service:

```bash
uv run bob restart
```

Skills can then use `os.environ.get("GOOGLE_PLACES_API_KEY")` or rely on SDK auto-detection.

## Testing

```bash
uv run pytest
```

## Data Storage

- Database: `~/data/bob.db`
- Config: `~/config/`
- Call recordings: `~/config/harness/calls/` (stereo 24kHz WAV)
- WhatsApp bridge data: `~/data/whatsappbridge/`
- Service: systemd user service (`bob.service`)
