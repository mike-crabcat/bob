---
name: cyborg-cli
description: "Interface with Cyborg for autonomous project execution and sending emails via agentmail."
---

# Cyborg CLI

**Always use the `cyborg` CLI. Never call the HTTP API directly.**

## Setup
Use `uv sync` to setup a venv, and then `uv tool install cyborg-server`

## Rules

** YOU ARE NOT THE DEVELOPER OF CYBORG ** If it has an error tell the user - don't try to fix it or hack it.

1. **Only execute work when dispatched.** You will receive task assignment notifications from Cyborg. Follow the instructions in the assignment prompt. Do not start work you were not assigned.
2. **Always include `--result-summary`** when completing tasks.
3. **Block tasks needing human input** — use `task block` with `--reason` and `--resume-instructions`. For structured questions, include `--input-schema-json` to create a dashboard approval the user can respond to. Do not leave tasks in `active` state waiting.
4. **Record all files created during task execution** — use `task file` to register every file produced.
5. **Store UUIDs** of created resources for later reference.
6. **Do not mention Cyborg internals** (task IDs, notification IDs, session keys) in user-facing messages unless the assignment prompt explicitly tells you to.

## Quick Reference

Use `uv run` to run all commands.  Use a `uv sync` in the skill directory to setup a venv for it.

| Need | Command |
|------|---------|
| Create project | `project create` (with aim, success-criteria-json) |
| Revise rejected spec | `project spec submit` with updated fields |
| Submit completed work | `task submit` (enters review) |
| Review submitted work | `task verify-submit` with OTP |
| Blocked task | `task block --reason --resume-instructions` → wait → `task unblock` |
| Structured input | `task block --reason --resume-instructions --input-schema-json '{...}'` |
| Record file | `task file --project-id --filename --purpose` |
| Check status | `context summary` |
| Send email | `email send --inbox <id> --to <addr> --subject <subj> --text <body>` |
| Reply to email | `email reply --inbox <id> --message-id <id> --text <reply>` |
| List email threads | `email threads [--inbox <id>]` |
| Get context for injection | `openclaw context` |
| Add to calendar | `event create` → `event recipient-add` |

## Projects

### Creating a Project

Project creation requires **aim** and **success criteria**. Method and plan are optional — Cyborg will generate a plan automatically after approval if you don't provide one.

```bash
uv run cyborg project create "Project Name" \
  --aim "What success looks like" \
  --success-criteria-json '[{"check":"output_exists","description":"Output file created"}]' \
  --description "What this project does" \
  --channel whatsapp \
  --chat-id <chat-id>
```

A spec (v1) is created automatically. The project waits for approval — do not start or execute the project yourself.

### After Rejection

If the spec is rejected, submit a revised version:

```bash
uv run cyborg project spec submit <project-id> \
  --aim "Updated aim" \
  --method "Updated method" \
  --success-criteria-json '[{"check":"...","description":"..."}]'
```

### Other Project Commands

```bash
uv run cyborg project list --state active     # List projects by state
uv run cyborg project get <id>                # View project details
uv run cyborg project tasks <id>              # Tasks within a project
uv run cyborg project pause <id>              # Pause work
```

Project states: `planning` → `active` → `paused` → `closed`

## Tasks

Tasks are created by Cyborg during project execution. You will receive assignment prompts telling you what to do.

### Task Lifecycle

```
planning → pending → active → completed / failed
                      ↓
                   blocked (waiting for input)
                      ↓
                    active (unblocked)
                      ↓
                   submitted (awaiting review)
```

```bash
# Lifecycle
uv run cyborg task start <id>                                 # pending → active
uv run cyborg task complete <id> --result-summary "Done"      # active → completed
uv run cyborg task block <id> --reason "Need X" --resume-instructions "When unblocked: 1. Get X. 2. Continue."
uv run cyborg task unblock <id>                               # Resume a blocked task
uv run cyborg task fail <id>                                  # Mark as failed

# List & query
uv run cyborg task list --status pending
uv run cyborg task list --status blocked
uv run cyborg task list --project-id <id>
```

### Submitting Work for Review

When you finish a task, submit it. Cyborg sends it for review and you will receive a one-time password (OTP).

```bash
uv run cyborg task submit <id> --result-summary "Summary of what was done"
```

If rejected, you will receive a retry notification with feedback. Address the issues and re-submit.

### Blocking for Structured User Input

When a task needs user input before it can continue, block it with an `input_schema`. This creates a dashboard approval the user can respond to.

**When to use:** Any time you need a specific answer from the user — choices, names, confirmations, preferences. Use plain `task block` (without schema) only when waiting on an external event.

**Flow:** `task block` with schema → dashboard shows approval → user responds → task auto-unblocks → you receive the answer

#### Text input

```bash
uv run cyborg task block <id> \
  --reason "Need a project name to proceed" \
  --resume-instructions "Use the provided name in the configuration file and continue setup." \
  --input-schema-json '{
    "type": "text",
    "prompt": "What should we name this project?",
    "placeholder": "Enter a project name..."
  }'
```

#### Multi-choice input

```bash
uv run cyborg task block <id> \
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

Add `"allow_multiple": true` for multi-select. Options can also include `image_url` and `audio_url` with project-relative paths (register files with `task file` first).

### Recording Task Files

**Every file created during task execution must be registered.**

```bash
uv run cyborg task file <task-id> \
  --project-id <project-id> \
  --filename "output.md" \
  --purpose result \
  --description "Analysis results"
```

File purposes: `reasoning`, `result`, `analysis`, `log`, `artifact`, `other`

## Contacts

```bash
uv run cyborg contact create "Name" --phone-number "+61456224867" --email "name@example.com"
uv run cyborg contact list
uv run cyborg contact get <id>
uv run cyborg contact update <id> --email "new@example.com"
uv run cyborg contact delete <id>
uv run cyborg contact by-phone "+61456224867"
uv run cyborg contact by-email "name@example.com"
```

## Calendar & Events

```bash
# Events
uv run cyborg event create "Meeting" --time "2026-04-05T10:00:00" --duration 30
uv run cyborg event create "Call" --time "now" --duration 15
uv run cyborg event create "Follow-up" --time "+2h" --venue "Office"
uv run cyborg event list
uv run cyborg event get <id>
uv run cyborg event update <id> --time "2026-04-05T14:00:00"
uv run cyborg event event delete <id>

# Add recipients
uv run cyborg event recipient-add <id> --address "email@example.com" --name "Alice"

# Confirm/cancel
uv run cyborg event confirm <id>
uv run cyborg event cancel <id>
```

- `--time` accepts ISO datetime, `"now"`, or relative like `"+1h"`, `"+30m"`
- `--duration` is in minutes, defaults to 60
- Default timezone: `Australia/Perth`

## Context

```bash
uv run cyborg context summary       # All active tasks + projects
uv run cyborg context tasks         # Task-focused context
uv run cyborg context projects      # Project-focused context
uv run cyborg openclaw context      # Plain text context for injection
```

## Email

Email relay via AgentMail. Each email thread maps to a session so replies share context.

### Sending a New Email

```bash
uv run cyborg email send --inbox <inbox-id> --to "recipient@example.com" --subject "Subject" --text "Body"
uv run cyborg email send --inbox <inbox-id> --to "a@example.com" --subject "Hello" --text "Hi" --cc "b@example.com"
```

### Replying to an Email Thread

When you receive an email task assignment, reply using the thread's message ID:

```bash
uv run cyborg email reply --inbox <inbox-id> --message-id <msg-id> --text "Reply text"
uv run cyborg email reply --inbox <inbox-id> --message-id <msg-id> --text "Reply" --reply-all
```

Use `--reply-all` to include all CC'd recipients.

### Inbox Management

```bash
uv run cyborg email inbox register --agentmail-inbox-id <id> --display-name "Name" --email-address "addr"
uv run cyborg email inbox list
uv run cyborg email inbox get <id>
uv run cyborg email inbox remove <id>
```

### Listing Messages and Threads

```bash
uv run cyborg email messages --inbox <inbox-id>
uv run cyborg email threads [--inbox <id>]
uv run cyborg email thread get <thread-id>
```
