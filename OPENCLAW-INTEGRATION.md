# OpenClaw Integration

This document describes the current Cyborg to OpenClaw integration.

## Purpose

Cyborg persists notifications and workflow state.
OpenClaw provides the live agent runtime and channel delivery.

The integration is used for:

- task assignment into a target DM or group session
- task and project prompts back to the source session
- task result/status updates
- calendar reminders

## Delivery Model

Cyborg now uses the OpenClaw gateway only.

- Visible user-facing delivery:
  - gateway `send` RPC
  - concrete `channel` + `to`
  - optional `sessionKey` so the sent message is mirrored into the correct OpenClaw session
- Source-routed notifications:
  - use gateway `send`
  - message text is rendered directly from the Cyborg notification
- Target task assignment:
  - gateway `agent` RPC
  - explicit target `sessionKey`
  - `deliver: true`
  - prompt carries the hidden task context and tells the agent to send the first natural user-facing message

That means target task assignment uses one agent turn: the hidden prompt lands in the target session, and the assistant's reply is what reaches the handset.

The implementation lives in [openclaw_hook_service.py](/home/mike/.openclaw/workspace/projects/cyborg/cyborg/services/openclaw_hook_service.py).

## Cyborg Config

Set these on the Cyborg process:

```bash
CYBORG_OPENCLAW_BASE_URL=https://openclaw.example
CYBORG_OPENCLAW_TOKEN=shared-secret
CYBORG_OPENCLAW_GATEWAY_URL=wss://openclaw.example
# Optional if gateway auth is disabled or shares the same token.
# CYBORG_OPENCLAW_GATEWAY_TOKEN=shared-secret
CYBORG_OPENCLAW_AGENT_ID=main
```

Notes:

- Cyborg loads `.env` automatically from the normal config search order.
- `CYBORG_OPENCLAW_GATEWAY_URL` defaults from `CYBORG_OPENCLAW_BASE_URL` by switching `http -> ws` or `https -> wss`.
- `CYBORG_OPENCLAW_GATEWAY_TOKEN` defaults to `CYBORG_OPENCLAW_TOKEN` if unset.
- `CYBORG_OPENCLAW_AGENT_ID` is optional but recommended so task-assignment agent turns run under the intended OpenClaw agent.

## OpenClaw Config

### Minimum Required Setup

Example `~/.openclaw/openclaw.json`:

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

Why these matter:

- `gateway.auth.token` must match `CYBORG_OPENCLAW_GATEWAY_TOKEN` or `CYBORG_OPENCLAW_TOKEN`.
- `session.dmScope: "per-channel-peer"` makes WhatsApp DMs use real per-user session keys like `agent:main:whatsapp:direct:+61400111222`.

HTTP hook setup is no longer required for Cyborg notification delivery.

### Channel Setup

OpenClaw must still be able to send on the channel you target.

For WhatsApp:

```bash
openclaw channels login --channel whatsapp --account default
openclaw channels status --probe
```

You also need:

- any required allowlists configured for DMs/groups
- target groups present in `channels.whatsapp.groups` if you use a group allowlist
- a stable WhatsApp listener; if `send` fails with `No active WhatsApp Web listener`, Cyborg cannot fix that transport failure

## Session Routing

### Source Session

Source routing comes from task/project/calendar metadata:

- `channel`
- `chat_id`
- `session_key`

That is where result notifications and “needs input” prompts go.

### Target Session

Task assignment can target another session with `metadata.target_session`.

Supported target kinds:

- `group`
- `dm`

Resolution rules:

- group targets need a concrete WhatsApp group `chat_id` or a registered `session-route`
- DM targets resolve from `target_session.contact_id`
- if no explicit DM session route exists, Cyborg derives the real OpenClaw session key automatically as:
  - `agent:<agent-id>:whatsapp:direct:+<e164>`

Examples:

- group session key:
  - `agent:main:whatsapp:group:120363426096069246@g.us`
- DM session key:
  - `agent:main:whatsapp:direct:+61400111222`

## Task Assignment Flow

When a task with a target session becomes `pending`:

1. Cyborg resolves the target route.
2. Cyborg calls the OpenClaw gateway `agent` method against the real target `sessionKey`.
3. That prompt tells the agent:
   - this session owns the Cyborg task
   - use the next user reply as task input
   - update/complete the Cyborg task when the answer is clear
   - send the first natural user-facing message now
4. OpenClaw delivers the agent's first reply directly to the handset or group.

Expected outcome:

- OpenClaw TUI/session history shows the hidden task-context prompt plus the visible assistant message
- the handset sees only the assistant's natural opening question, not a Cyborg wrapper message

If the handset sees `Task to action: ...`, Cyborg is still using an old build.

## Session Route Registry

Session routes are only needed when the route cannot be derived directly.

Typical uses:

- group targets
- explicit DM session overrides
- source sessions that need durable routing by logical `session_key`

Examples:

```bash
uv run cyborg session-route create whatsappgroup-bob-management \
  --kind group \
  --chat-id 120363426096069246@g.us

uv run cyborg session-route create agent:main:whatsapp:direct:+61400111222 \
  --kind dm \
  --contact-id <contact-id>
```

## Verification

Useful checks:

```bash
uv run cyborg notification list
uv run cyborg notification process-due
uv run cyborg session-route list
openclaw channels status --probe
openclaw gateway status --json
```

For a full task-assignment smoke test:

1. create a contact with a real phone number
2. create a task with `target_session.kind=dm` and that `contact_id`
3. approve the task plan so the task becomes `pending`
4. confirm:
   - Cyborg creates a `task_assignment` notification
   - OpenClaw shows the hidden task-context prompt in the DM session
   - WhatsApp receives only the assistant's natural opening question

## Troubleshooting

### `Task to action: ...` reaches WhatsApp

Cause:

- Cyborg is using the old wrapper-send path for target task assignment

Fix:

- restart `cyborg.service` on the updated build

### DM session key mismatch

Cause:

- OpenClaw DM sessions are using `:direct:` while Cyborg is deriving a different key shape

Fix:

- use the current Cyborg build
- keep `session.dmScope: "per-channel-peer"`

### Notification says delivered but WhatsApp got nothing

Meaning:

- Cyborg successfully handed the notification to OpenClaw
- OpenClaw transport failed afterward

Common cause:

- `No active WhatsApp Web listener (account: default)`

Fix:

- stabilize the OpenClaw WhatsApp listener
- verify with `openclaw channels status --probe`

### Group delivery works but DM assignment does not

Check:

- target contact exists and has a usable phone number
- derived DM session key matches the OpenClaw session naming scheme
- `CYBORG_OPENCLAW_AGENT_ID` matches the agent handling the DM session

## Operational Note

The OpenClaw hotfix for the duplicated active-listener map bug may still be required on affected OpenClaw builds. If direct `send` calls fail with `No active WhatsApp Web listener` while status probes look healthy, that is an OpenClaw transport/runtime issue rather than a Cyborg routing issue.
