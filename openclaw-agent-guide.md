# OpenClaw Agent Guide

This document explains how an OpenClaw agent should use Cyborg's project and task systems.

The important rule is: do not treat Cyborg as a free-form notes store. It has workflow rules now, and the agent should follow them exactly.

## Core Rules

### Projects

- A project is a container for work, routing metadata, journal history, and linked tasks.
- Approving a project spec automatically starts execution — no separate start or execute step is needed.
- A project spec contains the approved:
  - `aim`
  - `method`
  - `success_criteria`
  - optional execution `plan`

### Tasks

- Every task must be created with an initial `plan`.
- New tasks start in `planning`.
- A task cannot move to `pending` until its current plan is approved.
- A task cannot move to `active` until it is already `pending`.
- When completing a task, the result must be sent as `result_summary`.

## Correct Project Workflow

Use this sequence.

1. Create the project with aim and success criteria (spec v1 is created automatically).
2. Wait for user approval or rejection.
3. If rejected, revise and resubmit the project spec.
4. Once approved, Cyborg runs the project automatically.
5. Create and complete linked tasks as the project proceeds.

### 1. Create the project

Use `project create` with `--aim` and `--success-criteria-json`. Spec v1 is created automatically — you do not need a separate `spec submit` step.

Required:
- `title`
- `--aim`: what success means
- `--success-criteria-json`: explicit measurable criteria

Optional:
- `--method`: how the work will be approached (defaults to empty if not provided)
- `--plan-json`: execution plan (Cyborg can generate one via OpenClaw reasoning if omitted)
- `--description`: project description
- source routing metadata (`--channel`, `--session-key`, `--chat-id`)

It is fine and expected to submit only aim and success criteria. Plan and method are optional — Cyborg will handle plan generation after approval if needed.

CLI example:

```bash
uv run cyborg project create "Q1 Data Migration" \
  --aim "Migrate the customer records to the new schema without losing data." \
  --success-criteria-json '[{"check":"records_migrated > 0","description":"Customer records were migrated"},{"check":"failed_task_count == 0","description":"No migration task failed"}]' \
  --description "Move customer records to the new schema" \
  --channel whatsapp \
  --session-key whatsappgroup-main
```

With an optional plan:

```bash
uv run cyborg project create "Q1 Data Migration" \
  --aim "Migrate the customer records to the new schema without losing data." \
  --success-criteria-json '[{"check":"records_migrated > 0","description":"Customer records were migrated"},{"check":"failed_task_count == 0","description":"No migration task failed"}]' \
  --plan-json '[{"title":"Extract","description":"Export source data","criteria":"source export exists","order":0},{"title":"Transform","description":"Normalize the data","criteria":"normalized output exists","order":1},{"title":"Load","description":"Import into the new schema","criteria":"records imported","order":2}]'
```

API example:

```json
POST /api/v1/projects
{
  "title": "Q1 Data Migration",
  "aim": "Migrate the customer records to the new schema without losing data.",
  "success_criteria": [
    {
      "check": "records_migrated > 0",
      "description": "Customer records were migrated"
    }
  ]
}
```

### 2. Wait for approval

After creating the project:

- do not start the project
- do not execute the project
- do not assume the spec is approved
- wait for the user to approve or reject

Check spec status with:

```bash
uv run cyborg project spec list <project-id>
uv run cyborg project get <project-id>
```

Approval:

```bash
uv run cyborg project spec approve <project-id> --approver Mike
```

Rejection:

```bash
uv run cyborg project spec reject <project-id> --feedback "The success criteria are too vague. Add a clear verification condition."
```

### 3. Submit a revised spec (only if rejected)

Use `project spec submit` only when the user rejects the spec and changes are needed.

CLI example:

```bash
uv run cyborg project spec submit <project-id> \
  --aim "Revised aim" \
  --method "Revised method" \
  --success-criteria-json '[{"check":"revised_check","description":"Revised criteria"}]'
```

### 4. Execution starts automatically on approval

- do not start the project
- do not execute the project
- do not assume the draft spec is approved

Check spec status with:

```bash
uv run cyborg project spec list <project-id>
uv run cyborg project get <project-id>
```

Approval:

```bash
uv run cyborg project spec approve <project-id> --approver Mike
```

Rejection:

```bash
uv run cyborg project spec reject <project-id> --feedback "The success criteria are too vague. Add a clear verification condition."
```

If the spec is rejected:

- read the rejection feedback
- revise the spec
- submit a new version

### 4. Execution starts automatically on approval

Approving the spec automatically starts project execution. There is no separate `start` or `execute` command — spec approval triggers:
- Project state transition to `active`
- First task creation from the plan
- Notification sync

If the approved spec does not include an execution `plan`, Cyborg will ask OpenClaw to generate one and submit it as a new pending spec revision. The project will not start executing until the generated plan revision is approved by the user. Plan generation is an OpenClaw reasoning activity — it does not create tasks.

Just approve and the project runs (or wait for the generated plan if no plan was provided).

## What makes a good project

The agent should not submit weak projects.

### Aim (required)

Good aims are concrete and outcome-oriented.

Good:

- "Create a working family movie-night planning flow."
- "Produce a customer-ready proposal and send it."

Bad:

- "Work on the project."
- "Figure some things out."

### Success criteria (required)

Success criteria must be explicit enough that Cyborg or a reviewer can tell whether the project is done.

Good:

- "A final restaurant is selected and recorded."
- "The proposal has been sent to the customer."
- "At least one successful test run is recorded."

Bad:

- "It looks good."
- "We did enough."

Do not leave success criteria empty for projects you expect to start or execute.

### Method (optional)

If provided, the method should summarize the intended approach, not just restate the aim.

Good:

- "Collect venue options, compare them against the family's timing constraints, propose two choices, and confirm a final selection."

Bad:

- "Complete the project."

### Plan (optional)

An execution plan can be included, but is not required. If omitted, Cyborg will generate one via OpenClaw reasoning after the spec is approved.

## Correct Task Workflow

Tasks have a separate approval flow from projects.

1. Create the task with an initial `plan`.
2. If the plan changes, submit a revised plan.
3. Wait for plan approval.
4. Start only when the task is `pending`.
5. Complete with `result_summary`, or fail with `result`.

### Create a task

CLI:

```bash
uv run cyborg task create "Draft proposal" \
  --plan "1. Gather the requirements. 2. Draft the proposal. 3. Review and deliver." \
  --project-id <project-id> \
  --channel whatsapp \
  --session-key whatsappgroup-main
```

API:

```json
POST /api/v1/tasks
{
  "title": "Draft proposal",
  "plan": "1. Gather the requirements. 2. Draft the proposal. 3. Review and deliver.",
  "project_ids": ["<project-id>"],
  "metadata": {
    "channel": "whatsapp",
    "session_key": "whatsappgroup-main"
  }
}
```

### Submit and approve a revised task plan

```bash
uv run cyborg task plan submit <task-id> --content "1. Gather requirements. 2. Draft. 3. Review. 4. Deliver."
uv run cyborg task plan approve <task-id> --approver Mike
```

### Start, complete, and fail tasks

Start:

```bash
uv run cyborg task start <task-id>
```

Complete:

```bash
uv run cyborg task complete <task-id> --result-summary "Proposal sent to the customer."
```

Fail:

```bash
uv run cyborg task fail <task-id> --details-json '{"reason":"missing data"}' --result "Blocked by missing source records."
```

Use `result_summary` on completion. Do not use a made-up field name.

## Task submission review

When you call `cyborg task submit`, the task enters `submitted` status and Cyborg sends a review prompt to the agent session. The prompt includes a one-time password (OTP). You must review the work and then call the verification command.

Review and approve (the work is satisfactory):

```bash
cyborg task verify-submit <task-id> --otp <otp> --approve
```

Review and reject (issues found):

```bash
cyborg task verify-submit <task-id> --otp <otp> --reject --reason "Explain what is wrong"
```

API:

```
POST /api/v1/tasks/{task_id}/verify-submit
{"otp": "<otp>", "approved": true}
POST /api/v1/tasks/{task_id}/verify-submit
{"otp": "<otp>", "approved": false, "reason": "issues found", "issues": ["issue1"]}
```

The OTP is single-use. If rejected, the task returns to `active` and you receive a retry notification with feedback.

## Linking tasks to projects

If work belongs to a project, link it.

- create project tasks with `cyborg project task-create ...`
- or create regular tasks with `project_ids`

This ensures:

- project journal reconciliation works
- project autonomy can reason over linked tasks
- context summaries show the parent project name and id

## Dependencies

Use `parent_id` when one task depends on another.

Example:

- Task A: "Ask Alice for the quote"
- Task B: "Review Alice's quote"
- Task B should use `parent_id = Task A`

Important:

- dependency-blocked tasks should not be treated as ready to work
- once the parent finishes, Cyborg can release the child task automatically

## Cross-session task routing

Tasks can have:

- a source session
- a target session

Source session fields control where:

- approval prompts go
- planning questions go
- completion/result notifications go

Target session fields control where the task is actioned.

### Source session

Use task metadata fields:

- `channel`
- `session_key`
- `chat_id`

### Target session

Use task metadata `target_session`, or the CLI flags:

- `--target-kind group|dm`
- `--target-session-key`
- `--target-chat-id`
- `--target-contact-id`

DM guidance:

- use `target_contact_id` for direct messages
- do not guess raw phone numbers when a contact record should exist

Group guidance:

- use a real OpenClaw group session key or a concrete group `chat_id`

## Muting project notifications

When a project's tasks are malfunctioning and causing excessive notification noise in the target channel (e.g. a WhatsApp group), you can mute the project to stop all outbound notifications while the reasoning loop continues.

Muting only affects notification delivery. The reasoning loop, task execution, and autonomy decisions continue as normal — no messages reach the channel.

Mute:

```bash
uv run cyborg project mute <project-id>
```

API:

```
POST /api/v1/projects/{project_id}/mute
```

Unmute when the issue is resolved:

```bash
uv run cyborg project unmute <project-id>
```

API:

```
POST /api/v1/projects/{project_id}/unmute
```

Use mute when:

- Tasks are in a retry loop sending repeated notifications
- A task is generating garbled or incorrect messages
- The channel is being spammed and you need to stop the noise while investigating

Remember to unmute once the issue is resolved so normal notifications resume.

## What the agent should avoid

Do not:

- try to start or execute a project manually (spec approval handles this)
- execute a project with empty success criteria
- create tasks without a `plan`
- start a task directly from `planning`
- complete a task without a meaningful `result_summary`
- create project-related work as unlinked standalone tasks unless there is a strong reason

## Minimal safe recipes

### Safe project recipe

1. `project create --aim ... --success-criteria-json ...` (spec v1 auto-created)
2. wait for approval
3. Cyborg runs the project (or wait for generated plan if no plan was provided)
4. create linked tasks (or let auto-execution handle it)
5. complete tasks with `result_summary`

### Safe task recipe

1. `task create --plan ...`
2. if needed, `task plan submit`
3. wait for plan approval
4. `task start`
5. `task complete --result-summary ...`

## If unsure

If the user request is vague:

- ask clarifying questions before submitting the project spec
- prefer a better draft spec over a premature one
- do not hide uncertainty inside vague success criteria

The project system is only useful if the approved `aim` and `success_criteria` are explicit enough to govern later autonomy.
