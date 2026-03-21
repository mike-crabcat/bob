# OpenClaw Agent Guide

This document explains how an OpenClaw agent should use Cyborg's project and task systems.

The important rule is: do not treat Cyborg as a free-form notes store. It has workflow rules now, and the agent should follow them exactly.

## Core Rules

### Projects

- A project is a container for work, routing metadata, journal history, and linked tasks.
- A project must not be started or auto-executed until it has an approved project spec.
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

1. Create the project shell.
2. Submit a project spec.
3. Wait for user approval or rejection.
4. If rejected, revise and resubmit the project spec.
5. Only after approval, start the project or execute it.
6. Create and complete linked tasks as the project proceeds.

### 1. Create the project shell

Use project create for:

- `title`
- `description`
- source routing metadata
- optional `auto_execute`

Do not rely on `project create` alone to make the project executable.

CLI example:

```bash
uv run cyborg project create "Q1 Data Migration" \
  --description "Move customer records to the new schema" \
  --channel whatsapp \
  --session-key whatsappgroup-main
```

### 2. Submit a project spec

Use `project spec submit` once you have a concrete proposal for the work.

The spec must include:

- `aim`: what success means
- `method`: how the work will be approached
- `success_criteria`: explicit measurable criteria
- optional `plan`: a first-pass ordered execution outline

CLI example:

```bash
uv run cyborg project spec submit <project-id> \
  --aim "Migrate the customer records to the new schema without losing data." \
  --method "Export the legacy records, transform them, import them into the new schema, and verify record counts and sample rows." \
  --success-criteria-json '[{"check":"records_migrated > 0","description":"Customer records were migrated"},{"check":"failed_task_count == 0","description":"No migration task failed"}]' \
  --plan-json '[{"title":"Extract","description":"Export source data","criteria":"source export exists","order":0},{"title":"Transform","description":"Normalize the data","criteria":"normalized output exists","order":1},{"title":"Load","description":"Import into the new schema","criteria":"records imported","order":2}]'
```

API example:

```json
POST /api/v1/projects/{project_id}/specs
{
  "aim": "Migrate the customer records to the new schema without losing data.",
  "method": "Export, transform, import, and verify the records.",
  "success_criteria": [
    {
      "check": "records_migrated > 0",
      "description": "Customer records were migrated"
    }
  ],
  "plan": [
    {
      "title": "Extract",
      "description": "Export source data",
      "criteria": "source export exists",
      "order": 0
    }
  ]
}
```

### 3. Approval and rejection

After submitting a project spec:

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

### 4. Start or execute only after approval

Manual start:

```bash
uv run cyborg project start <project-id>
```

Auto-execution:

```bash
uv run cyborg project update <project-id> --auto-execute
uv run cyborg project execute <project-id>
```

If you try to start or execute without an approved spec, Cyborg will reject the request.

## What makes a good project spec

The agent should not submit weak specs.

### Aim

Good aims are concrete and outcome-oriented.

Good:

- "Create a working family movie-night planning flow."
- "Produce a customer-ready proposal and send it."

Bad:

- "Work on the project."
- "Figure some things out."

### Method

The method should summarize the intended approach, not just restate the aim.

Good:

- "Collect venue options, compare them against the family's timing constraints, propose two choices, and confirm a final selection."

Bad:

- "Complete the project."

### Success criteria

Success criteria must be explicit enough that Cyborg or a reviewer can tell whether the project is done.

Good:

- "A final restaurant is selected and recorded."
- "The proposal has been sent to the customer."
- "At least one successful test run is recorded."

Bad:

- "It looks good."
- "We did enough."

Do not leave success criteria empty for projects you expect to start or execute.

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

## What the agent should avoid

Do not:

- start a project before its spec is approved
- execute a project with empty success criteria
- create tasks without a `plan`
- start a task directly from `planning`
- complete a task without a meaningful `result_summary`
- create project-related work as unlinked standalone tasks unless there is a strong reason

## Minimal safe recipes

### Safe project recipe

1. `project create`
2. `project spec submit`
3. wait for approval
4. `project start` or `project execute`
5. create linked tasks
6. complete tasks with `result_summary`

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

The project system is only useful if the approved `aim`, `method`, and `success_criteria` are explicit enough to govern later autonomy.
