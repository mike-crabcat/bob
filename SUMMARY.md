# Cyborg

## Project Metadata

- **ID:** `69942812-6ff1-4dd9-92d0-242e832d58e8`
- **State:** active
- **Aim:** Build a complete SQLite-backed HTTP data service for Bob (OpenClaw bot) providing persistent storage for tasks, projects, and calendars with full CRUD operations, retry loops, recurring tasks, and context injection endpoints.
- **Method:** 1) Core data models and SQLite schema. 2) FastAPI HTTP API with CRUD operations. 3) Typer CLI for local management. 4) Project-task relationships and journaling. 5) Context injection endpoints for OpenClaw. 6) Plan versioning and approval workflow. 7) Automatic SUMMARY.md generation. 8) systemd service integration.
- **Description:** FastAPI-based service with Pydantic v2 models, aiosqlite database, Typer CLI tool, and systemd integration. Supports hierarchical tasks with subtasks, project journaling, calendar events with recipients, and automated context summaries for Bob memory window.
- **Created:** 2026-03-10 13:14:37.693702+00:00
- **Started:** 2026-03-10 13:18:56.105942+00:00

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

## Linked Tasks

### Add project method field and workflow enforcement

- **ID:** `85673d1f-7efd-4457-bc03-823e6afcb1f7`
- **Status:** completed
- **Priority:** high
- **Description:** Add a 'method' field to projects to store the iterative plan for achieving the aim. Update models, API, CLI, and skill documentation to enforce the Aim → Method → Tasks workflow.
- **Requested By:** Mike
- **Created:** 2026-03-12 11:26:12.082307+00:00
- **Completed:** 2026-03-12 11:29:25.370930+00:00
