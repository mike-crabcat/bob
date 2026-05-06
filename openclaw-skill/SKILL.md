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
| Send email | `email send --inbox <id> --to <addr> --subject <subj> --text <body> --agenda <purpose>` |
| Make phone call | `call <number> --agenda "Purpose of the call"` |
| Reply to email | `email reply --inbox <id> --message-id <id> --text <reply>` |
| Send with attachment | `email send ... --attach /path/to/file` |
| Send with inline image | `email send ... --html '<img src="cid:image.png" />' --inline-image /path/to/image.png` |
| List email threads | `email threads [--inbox <id>]` |
| Download attachment | `email download-attachment --inbox <id> --message-id <id> --attachment-id <id> --output <path>` |
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

### Thread Agenda

Every email thread has an **agenda** — a statement of the conversation's purpose and how responses should be handled. The agenda persists across all messages in the thread and guides how the agent processes replies.

**You MUST provide `--agenda` when sending a new email.** It answers "why are you sending this email?" beyond what the body says, and provides rules for handling responses.

**What to include:**
- The purpose of the conversation (what outcome you expect)
- How to handle replies (what to do if the recipient asks X, agrees, declines, etc.)
- Any special rules (tone, escalation, what to collect)

**Examples:**

```bash
# Scheduling a meeting
uv run cyborg email send --inbox <id> --to "alice@example.com" --subject "Meeting request" \
  --text "Hi Alice, can we meet next week?" \
  --agenda "Schedule a 30-minute meeting with Alice for next week. Preferred times: Tuesday or Wednesday afternoon. If she suggests alternatives, negotiate and confirm. If she declines, ask for a reason and report back."

# Sending a document for review
uv run cyborg email send --inbox <id> --to "bob@example.com" --subject "Q3 report for review" \
  --text "Hi Bob, please find the attached Q3 report." \
  --agenda "Bob is reviewing the Q3 financial report. If he has questions, answer them or escalate to the user. If he requests changes, note them and inform the user. Confirm receipt of his feedback." \
  --attach /path/to/report.pdf

# Collecting information
uv run cyborg email send --inbox <id> --to "vendor@example.com" --subject "Pricing request" \
  --text "Hi, could you send me your current pricing for the Enterprise plan?" \
  --agenda "Collect pricing details for the Enterprise plan. Record all numbers, terms, and conditions. If they offer a call instead, accept and report back the details. If they need more info about our requirements, ask the user."
```

For incoming emails that start new threads, a default agenda is used automatically. If the user asks to change the agenda for an existing thread:

```bash
uv run cyborg email update-agenda <thread-id> --agenda "New agenda text"
```

### Sending a New Email

```bash
uv run cyborg email send --inbox <inbox-id> --to "recipient@example.com" --subject "Subject" --text "Body" \
  --agenda "Purpose and handling instructions for this thread"
uv run cyborg email send --inbox <inbox-id> --to "a@example.com" --subject "Hello" --text "Hi" \
  --agenda "Greet and establish contact" --cc "b@example.com"
```

### Sending with Attachments

Use `--attach` to add file attachments. Repeat the flag for multiple files.

```bash
uv run cyborg email send --inbox <inbox-id> --to "recipient@example.com" --subject "Report" \
  --text "Please find the attached report." \
  --agenda "Deliver the attached report and confirm receipt." \
  --attach /path/to/report.pdf

# Multiple attachments
uv run cyborg email send --inbox <inbox-id> --to "a@example.com" --subject "Files" \
  --text "Here are the files." \
  --agenda "Deliver the requested files." \
  --attach /path/to/file1.pdf --attach /path/to/file2.xlsx
```

### Sending with Inline Images

To embed images directly in the email body, use `--inline-image` together with `--html`.

The `--html` body references each image using `cid:<filename>` where `<filename>` matches the basename of the file passed to `--inline-image`.

```bash
uv run cyborg email send --inbox <inbox-id> --to "a@example.com" --subject "Chart" \
  --text "See the chart below." \
  --agenda "Share the chart and gather feedback." \
  --html '<p>Here is the chart:</p><img src="cid:chart.png" />' \
  --inline-image /path/to/chart.png
```

You can mix `--attach` and `--inline-image` in the same command.

### Best Practices for Attachments and Images

- **Always include `--text`** as a plain-text fallback when using `--html`.
- **Inline images need both `--html` and `--inline-image`** — the `cid:` reference in the HTML must match the filename.
- **Don't send images in the first email to a new contact** — this hurts deliverability.
- **Keep attachments under 10 MB** — large files may cause timeouts.
- **Use `--html` for rich formatting** — styled text, tables, embedded images. Keep `--text` as a readable summary.

### Replying to an Email Thread

**You MUST always use `email reply` to respond to emails. Never use `email send` to reply to an existing thread.**

The `email reply` command threads your response into the existing conversation. The `--message-id` is provided in every incoming email prompt — use the exact value from the prompt.

If you cannot find the message ID in the prompt or conversation context, **ask the user for it** rather than guessing or falling back to `email send`.

```bash
uv run cyborg email reply --inbox <inbox-id> --message-id <msg-id> --text "Reply text"
uv run cyborg email reply --inbox <inbox-id> --message-id <msg-id> --text "Reply" --reply-all
```

Use `--reply-all` to include all CC'd recipients.

Reply with attachments using `--attach`, or reply with inline images using `--html` + `--inline-image`:

```bash
uv run cyborg email reply --inbox <inbox-id> --message-id <msg-id> \
  --text "Here is the signed document." \
  --attach /path/to/signed.pdf

uv run cyborg email reply --inbox <inbox-id> --message-id <msg-id> \
  --text "See the screenshot." \
  --html '<img src="cid:screenshot.png" />' \
  --inline-image /path/to/screenshot.png
```

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

### Downloading Attachments

When an email has attachments that were not auto-downloaded (e.g., from an untrusted sender), download individual attachments after reviewing their metadata:

```bash
uv run cyborg email download-attachment --inbox <inbox-id> --message-id <msg-id> --attachment-id <att-id> --output /path/to/save
```

- `--inbox`: The inbox ID (provided in the email prompt)
- `--message-id`: The message ID from the email prompt (angle-bracketed string)
- `--attachment-id`: The specific attachment ID (listed in the email prompt)
- `--output` / `-o`: Where to save the file. Defaults to current directory with attachment ID as filename.

Only download attachments after reviewing metadata and determining they are safe.

## Phone Calls

Initiate an outbound phone call. The voice assistant answers and follows the agenda throughout the conversation.

**You MUST provide `--agenda` when making a call.** The agenda tells the voice assistant the purpose of the call and how to handle the conversation.

**What to include:**
- The purpose of the call (what outcome you expect)
- How to handle responses (what to do if the person agrees, declines, asks questions, etc.)
- Any specific information to collect or convey

**Examples:**

```bash
# Scheduling a meeting
uv run cyborg call "+61456224867" \
  --agenda "Call to schedule a 30-minute meeting with this contact for next week. Preferred times: Tuesday or Wednesday afternoon. If they suggest alternatives, negotiate and confirm. If they decline, ask for a reason and report back."

# Following up on an email
uv run cyborg call "+61400111222" \
  --agenda "Follow up on the Q3 report email sent yesterday. Ask if they have had a chance to review it. If they have questions, answer them. If they need more time, note that and report back. If they have feedback, record it in detail."

# Collecting information
uv run cyborg call "+61456224867" \
  --agenda "Call to confirm the delivery address and time for order #12345. Verify the street address, unit number, and preferred delivery window. If no answer, report back."
```

### How It Works

1. You run `cyborg call` with the phone number and agenda
2. The system initiates a Twilio call to the number
3. When the person answers, the voice assistant (powered by OpenClaw) begins the conversation guided by your agenda
4. The assistant stays on topic and works toward the agenda's goal throughout the call
