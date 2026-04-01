# Cyborg

## Project Metadata

- **ID:** `69942812-6ff1-4dd9-92d0-242e832d58e8`
- **State:** active
- **Aim:** Build complete SQLite-backed HTTP data service for persistent task, project, and calendar storage with OpenClaw integration
- **Method:** 1) Core data models and SQLite schema. 2) FastAPI HTTP API with CRUD. 3) Typer CLI tool. 4) Project-task relationships. 5) Context injection endpoints. 6) Plan versioning and approval workflow. 7) Auto documentation. 8) systemd integration.
- **Description:** FastAPI-based service with Pydantic v2 models, aiosqlite database, Typer CLI tool, and systemd integration. Supports hierarchical tasks with subtasks, project journaling, calendar events with recipients, and automated context summaries for Bob memory window.
- **Created:** 2026-03-10 13:14:37.693702+00:00
- **Started:** 2026-03-10 13:18:56.105942+00:00
- **Auto Execute:** No

## Linked Tasks

### Implement heartbeat-driven plan approval workflow

- **ID:** `c2a5f65a-df3e-446b-8ad6-56102538aa8c`
- **Status:** completed
- **Priority:** high
- **Description:** Create a system where heartbeat queries pending tasks without plans, Bob proposes plans to the user for approval, and approved plans auto-start tasks. Includes: HEARTBEAT.md updates, plan proposal generation, message routing for approve/reject, and auto-start on approval.
- **Requested By:** Mike
- **Created:** 2026-03-12 11:43:55.199497+00:00
- **Completed:** 2026-03-12 11:48:14.209265+00:00

### OpenClaw plugin integration for Cyborg

- **ID:** `a9ef72ed-dd82-496c-933a-86ca7c48c613`
- **Status:** active
- **Priority:** high
- **Description:** Create a native OpenClaw plugin for Cyborg that provides context injection via the plugin system instead of HTTP polling. Plugin should use before_prompt_build hook to inject active tasks, projects, and calendar events into Bob's context window. See tasks/af7fc85e-cf51-4d38-9a9c-8c08f1c5c291/plan.md for prior research.
- **Requested By:** Mike
- **Created:** 2026-03-12 11:34:04.316530+00:00

### Add cyborg project update CLI command

- **ID:** `382b5c71-0b65-4edb-96e6-20900c5116e7`
- **Status:** active
- **Priority:** high
- **Description:** Add 'cyborg project update' command to the CLI to allow updating project fields (aim, method, description, state) without using the API directly. Should support --aim, --method, --description, and --state options.
- **Requested By:** Mike
- **Created:** 2026-03-12 11:34:04.011301+00:00

### Add project method field and workflow enforcement

- **ID:** `85673d1f-7efd-4457-bc03-823e6afcb1f7`
- **Status:** completed
- **Priority:** high
- **Description:** Add a 'method' field to projects to store the iterative plan for achieving the aim. Update models, API, CLI, and skill documentation to enforce the Aim → Method → Tasks workflow.
- **Requested By:** Mike
- **Created:** 2026-03-12 11:26:12.082307+00:00
- **Completed:** 2026-03-12 11:29:25.370930+00:00

## Plan Progress

✅ **Step 1:** Data models
   - Create Pydantic models and SQLite schema
   - *Criteria:* Models defined

✅ **Step 2:** FastAPI
   - Build FastAPI HTTP API with CRUD operations
   - *Criteria:* API serving

🔄 **Step 3:** CLI tool
   - Build Typer CLI for local management
   - *Criteria:* CLI functional

⏳ **Step 4:** Relationships
   - Add project-task relationships and journaling
   - *Criteria:* Relations working

⏳ **Step 5:** Context endpoints
   - Build OpenClaw context injection endpoints
   - *Criteria:* Context flowing

⏳ **Step 6:** Approval workflow
   - Implement plan versioning and approval
   - *Criteria:* Workflow working

**Progress:** 2/6 steps completed

## Success Criteria

- **FastAPI serving requests**
  - Check: `api_working`
- **Typer CLI functional**
  - Check: `cli_working`
- **OpenClaw context endpoints working**
  - Check: `context_injection`

## Journal Entries

### Milestone - 2026-03-10 13:40:56.251353+00:00

Added OpenClaw plugin integration for automatic context injection. New endpoints provide formatted summaries of active projects, tasks, and events.

**Metadata:**
- `feature`: openclaw_plugin

### Milestone - 2026-03-10 13:54:04.703760+00:00

Fixed enum bug - removed use_enum_values from CyborgModel

**Metadata:**
- `fix`: enum_handling

### Result - 2026-03-11 12:37:52.872549+00:00

Task completed: Delete obsolete skill: /projects/cyborg/openclaw-plugin/

Result: Deleted /projects/cyborg/openclaw-plugin/ directory using trash-put

**Metadata:**
- `task_id`: eaff2686-e98b-4eff-9d5d-a199e070d17a
- `task_title`: Delete obsolete skill: /projects/cyborg/openclaw-plugin/

### Result - 2026-03-11 21:22:36.284324+00:00

Task completed: Investigate OpenClaw plugin system and propose Cyborg integration plan

Result: Completed research on OpenClaw plugin architecture. Key findings: (1) Plugin manifest (openclaw.plugin.json) required for all plugins, (2) before_prompt_build hook can inject context via prependSystemContext for caching efficiency, (3) context-engine slot available for exclusive context providers, (4) Optional tools can be registered for task/project CRUD. Created comprehensive implementation plan at tasks/af7fc85e-cf51-4d38-9a9c-8c08f1c5c291/plan.md with: research findings, proposed plugin design with caching strategy, 4-phase implementation plan, configuration examples, and future enhancement ideas.

**Metadata:**
- `task_id`: af7fc85e-cf51-4d38-9a9c-8c08f1c5c291
- `task_title`: Investigate OpenClaw plugin system and propose Cyborg integration plan

### Milestone - 2026-03-11 22:36:09.469356+00:00

Implemented plan versioning system for tasks. New plans table with version tracking, approval workflow, and blocking behavior. Tasks cannot start until plan is approved. Includes APIs for submit/approve/reject plans.

**Metadata:**
- `files_created`: ['cyborg/schemas/40_plans.sql', 'cyborg/routers/plans.py', 'cyborg/services/plan_service.py']
- `files_modified`: ['cyborg/models.py', 'cyborg/dependencies.py', 'cyborg/main.py', 'cyborg/services/task_service.py']
- `implemented_by`: subagent

### Milestone - 2026-03-11 22:57:47.809535+00:00

Implemented automatic SUMMARY.md generation for projects. When projects are created, updated, or have journal entries added, a human-readable markdown summary is written to /home/mike/.openclaw/workspace/projects/{project-slug}/SUMMARY.md. Includes project metadata, journal entries, and linked tasks. Enables memory search integration.

**Metadata:**
- `features`: ['Automatic SUMMARY.md generation', 'Slug-based directory naming', 'Complete markdown report with metadata, journal, tasks', 'Synchronous file writes on all project changes']
- `files_modified`: ['cyborg/services/project_service.py', 'cyborg/routers/projects.py']
- `implemented_by`: subagent

### Note - 2026-03-12 11:14:49.684165+00:00

Testing automated summary generation for Bibliotheca docs. This entry should trigger a new SUMMARY.md export.

### Note - 2026-03-12 11:15:57.276920+00:00

Second test entry to trigger SUMMARY.md generation.

### Note - 2026-03-12 11:17:23.531351+00:00

Third test entry after Cyborg restart.

### Milestone - 2026-03-12 11:21:05.703622+00:00

Updated project aim and description with detailed requirements from original PROMPT.md.

**Metadata:**
- `source`: PROMPT.md
- `updated_fields`: ['aim', 'description']

### Result - 2026-03-12 11:29:25.370930+00:00

Task completed: Add project method field and workflow enforcement

Result: Added method field to Project model, database schema (25_add_project_method.sql), API service, CLI (project create/get), and skill documentation. Tested successfully.

**Metadata:**
- `task_id`: 85673d1f-7efd-4457-bc03-823e6afcb1f7
- `task_title`: Add project method field and workflow enforcement

### Result - 2026-03-12 11:48:14.209265+00:00

Task completed: Implement heartbeat-driven plan approval workflow

Result: Implemented heartbeat-driven plan approval workflow: 1) Updated HEARTBEAT.md with new workflow, 2) Added cyborg task plan submit/approve/reject/list CLI commands, 3) Fixed UUID import in plan_service.py, 4) Updated skill documentation with plan commands, 5) Tested complete workflow: submit plan → approve → start task.

**Metadata:**
- `task_id`: c2a5f65a-df3e-446b-8ad6-56102538aa8c
- `task_title`: Implement heartbeat-driven plan approval workflow

### Note - 2026-03-30 12:02:53.925749+00:00

Cyberpunk lobster logo added to project assets. Generated using OpenAI gpt-image-1. Features chrome hydraulic claws, glowing cybernetic eyes, neon magenta/cyan LED strips, rain-soaked cyberpunk alleyway background, and CYBORG text with glitch-effect neon font. Saved to projects/cyborg/assets/cyborg-lobster-cyberpunk.png. Requested by Mike.
