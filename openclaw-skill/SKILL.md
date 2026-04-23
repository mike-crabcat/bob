---
name: cyborg-cli
description: "Interface with Cyborg for autonomous project execution and task management. Use the CLI for all operations."
---

# Cyborg CLI

## Cyborg-OpenClaw Relationship

**Cyborg drives. OpenClaw follows.**

- Cyborg decides what to work on, when, and in what order
- OpenClaw must **never** start working on a task unless explicitly asked to by cyborg
- User requests are always to **set up** work — never to immediately start executing it
- OpenClaw's role is to prepare, plan, and record — not to autonomously begin work

**Always use the `cyborg` CLI. Never call the HTTP API directly.**

## Rules

1. **Never work on a task without being asked.** Only start a task when cyborg explicitly assigns it. User messages are instructions to set up work, not to execute it.
2. **Always pass `--channel whatsapp --chat-id <chat-id>`** when creating projects. Tasks inherit routing from their project.
3. **Always include `--result-summary`** when completing tasks.
4. **Block tasks needing human input** — use `task block` with `--reason` and `--resume-instructions`. For structured questions, include `--input-schema-json` to create a dashboard approval the user can respond to. Do not leave tasks in `active` state waiting.
5. **Record all files created during task execution** — use `task file` to register every file produced.
6. **Store UUIDs** of created resources for later reference.

## Quick Reference

| Need | Command |
|------|---------|
| New project | `project create` (with aim, success-criteria-json) |
| Modify existing project | `project pause` → `spec submit` → `spec approve` |
| Blocked task | `task block --reason --resume-instructions` → wait → `task unblock` |
| Structured input | `task block --reason --resume-instructions --input-schema-json '{...}'` → dashboard approval → `task unblock` |
| Record file | `task file --project-id --filename --purpose` |
| Check status | `context summary` |
| Get context for injection | `openclaw context` |
| Add to calendar | `event create` → `event recipient-add` |

## Creating a Project

Project creation requires **aim** and **success criteria**. Method is optional — if not provided, Cyborg's planning reasoning will generate it.

```bash
cyborg project create "Project Name" \
  --aim "What success looks like" \
  --success-criteria-json '[{"check":"output_exists","description":"Output file created"}]' \
  --description "What this project does" \
  --channel whatsapp \
  --chat-id <chat-id>
```

Optional: include `--method "How to execute"` to specify the approach upfront.

Additional project commands:
```bash
cyborg project list --state active     # List projects by state
cyborg project get <id>                # View project details
cyborg project tasks <id>              # Tasks within a project
cyborg project pause <id>              # Pause work
cyborg project close <id> --conclusion "Done"  # Close with conclusion
```

Project states: `planning` → `active` → `paused` → `closed`

## Modifying an Existing Project

Specs are **only** for modifying an already-existing project, and can **only** be submitted when the project is paused.

```bash
# 1. Pause the project
cyborg project pause <id>

# 2. Submit spec (aim, method, success-criteria-json all required)
cyborg project spec submit <id> \
  --aim "Updated aim" \
  --method "Updated method" \
  --success-criteria-json '[{"check":"output_exists","description":"Output file created"}]'

# 3. Approve spec (execution resumes automatically)
cyborg project spec approve <id> --approver "Mike"
```

## Tasks

Tasks are created by the cyborg service during project execution. Use lifecycle commands to manage them.

```bash
# Lifecycle
cyborg task start <id>                                 # pending → active
cyborg task complete <id> --result-summary "Done"      # active → completed
cyborg task block <id> --reason "Need X" --resume-instructions "When unblocked: 1. Get X. 2. Continue."
cyborg task block <id> --reason "Need choice" --resume-instructions "Use the answer" \
  --input-schema-json '{"type":"multi_choice","prompt":"Pick one","options":[{"value":"a","label":"Option A"},{"value":"b","label":"Option B"}]}'
cyborg task unblock <id>                               # Resume a blocked task
cyborg task fail <id>                                  # Mark as failed

# List & query
cyborg task list --status pending
cyborg task list --status blocked
cyborg task list --project-id <id>
```

Task statuses: `pending` → `active` → `completed` / `failed` / `blocked`

### Blocking for Structured User Input

When a task needs user input before it can continue, block it with an `input_schema`. This creates a `task_input` approval in the dashboard that the user can respond to.

**When to use:** Any time you need a specific answer from the user to proceed — choices, names, confirmations, preferences, etc. Use plain `task block` (without schema) only when you're waiting on an external event or unstructured conversation.

**Flow:** `task block` with schema → dashboard shows approval → user responds → task auto-unblocks → you receive the answer

There are two schema types:

#### Text input

For free-text questions (names, descriptions, URLs, etc.):

```bash
cyborg task block <id> \
  --reason "Need a project name to proceed" \
  --resume-instructions "Use the provided name in the configuration file and continue setup." \
  --input-schema-json '{
    "type": "text",
    "prompt": "What should we name this project?",
    "placeholder": "Enter a project name..."
  }'
```

Schema fields:
- `type` — always `"text"`
- `prompt` — the question to show the user (required)
- `placeholder` — hint text in the input field (optional)

#### Multi-choice input

For selecting from a fixed set of options:

```bash
cyborg task block <id> \
  --reason "Need to confirm deployment target" \
  --resume-instructions "Deploy to the selected environment." \
  --input-schema-json '{
    "type": "multi_choice",
    "prompt": "Which environment should we deploy to?",
    "options": [
      {"value": "staging", "label": "Staging"},
      {"value": "production", "label": "Production"}
    ]
  }'
```

For multiple selections, add `"allow_multiple": true`:

```bash
cyborg task block <id> \
  --reason "Need feature selection" \
  --resume-instructions "Enable the selected features." \
  --input-schema-json '{
    "type": "multi_choice",
    "prompt": "Which features should be enabled?",
    "options": [
      {"value": "auth", "label": "Authentication"},
      {"value": "logging", "label": "Audit Logging"},
      {"value": "notifications", "label": "Push Notifications"}
    ],
    "allow_multiple": true
  }'
```

Schema fields:
- `type` — always `"multi_choice"`
- `prompt` — the question to show the user (required)
- `options` — array of `{value, label}` objects (required, at least one)
- `allow_multiple` — allow selecting more than one option (optional, default false)

#### Multi-choice with images and audio

Options can include `image_url` and `audio_url` to present media inline in the dashboard approval. Use project-relative paths — the dashboard resolves them to full URLs automatically.

**Image options** — present generated images for the user to pick from:

```bash
cyborg task block <id> \
  --reason "Need to choose a design" \
  --resume-instructions "Use the selected design." \
  --input-schema-json '{
    "type": "multi_choice",
    "prompt": "Which design do you prefer?",
    "options": [
      {"value": "a", "label": "Design A", "image_url": "tasks/abc1/design_a.png"},
      {"value": "b", "label": "Design B", "image_url": "tasks/abc1/design_b.png"}
    ]
  }'
```

**Audio options** — present MP3 clips for the user to listen to and choose:

```bash
cyborg task block <id> \
  --reason "Need to select a voice clip" \
  --resume-instructions "Use the selected clip." \
  --input-schema-json '{
    "type": "multi_choice",
    "prompt": "Which voice do you prefer?",
    "options": [
      {"value": "voice1", "label": "Voice A", "audio_url": "tasks/abc1/voice_a.mp3"},
      {"value": "voice2", "label": "Voice B", "audio_url": "tasks/abc1/voice_b.mp3"}
    ]
  }'
```

**Combined** — an option can have both an image and audio:

```bash
{"value": "full", "label": "With media", "image_url": "tasks/abc1/preview.png", "audio_url": "tasks/abc1/preview.mp3"}
```

Media fields (both optional):
- `image_url` — relative path to an image file in the project workspace
- `audio_url` — relative path to an MP3 file in the project workspace

Paths must be relative (no `..` or leading `/`). Files must exist in the project workspace and should be registered with `task file` first.

After the user responds via the dashboard, the task is unblocked and you receive a notification with their answer. Use it to continue the task.

### Recording Task Files

**Every file created during task execution must be registered.** This tracks what was produced and why.

```bash
cyborg task file <task-id> \
  --project-id <project-id> \
  --filename "output.md" \
  --purpose result \
  --description "Analysis results"
```

File purposes:
- `reasoning` — thought process, planning documents
- `result` — primary output/deliverable
- `analysis` — data analysis, CSVs, reports
- `log` — execution logs
- `artifact` — other produced files (default)
- `other` — anything else

## Contacts

```bash
cyborg contact create "Name" --phone-number "+61456224867" --email "name@example.com"
cyborg contact list
cyborg contact get <id>
cyborg contact update <id> --email "new@example.com"
cyborg contact delete <id>
cyborg contact by-phone "+61456224867"
cyborg contact by-email "name@example.com"
```

## Calendar & Events

```bash
# Events
cyborg event create "Meeting" --time "2026-04-05T10:00:00" --duration 30
cyborg event create "Call" --time "now" --duration 15
cyborg event create "Follow-up" --time "+2h" --venue "Office"
cyborg event list
cyborg event get <id>
cyborg event update <id> --time "2026-04-05T14:00:00"
cyborg event delete <id>

# Add recipients
cyborg event recipient-add <id> --address "email@example.com" --name "Alice"

# Confirm/cancel
cyborg event confirm <id>
cyborg event cancel <id>
```

- `--time` accepts ISO datetime, `"now"`, or relative like `"+1h"`, `"+30m"`
- `--duration` is in minutes, defaults to 60
- Default timezone: `Australia/Perth`

## Context

```bash
cyborg context summary       # All active tasks + projects
cyborg context tasks         # Task-focused context
cyborg context projects      # Project-focused context
cyborg openclaw context      # Plain text context for injection
```
