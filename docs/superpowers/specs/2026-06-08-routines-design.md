# Routines — Scheduled Session Prompts

## Problem

Sessions have no way to wake up on a schedule. If an agent should gather tech news every weekday morning, the only option is for a user to message it manually each time.

## Solution

Routines are cron-scheduled prompts injected into existing sessions. The agent manages them via `read_routine()` / `write_routine()` / `delete_routine()` tools scoped to the current session. When a routine fires, the prompt is dispatched to the LLM independently — it does not block or serialize with other session activity.

## Data Model

New `routines` table:

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PK | UUID |
| session_key | TEXT | Which session this routine belongs to |
| name | TEXT | Unique identifier within a session (e.g. "morning-tech-news") |
| schedule | TEXT | 5-field cron expression |
| prompt | TEXT | The prompt to inject when the routine fires |
| enabled | INTEGER | 1 = active, 0 = paused |
| next_run_at | TEXT | ISO 8601 timestamp of next scheduled fire |
| last_run_at | TEXT | ISO 8601 timestamp of last successful run |
| created_at | TEXT | Creation timestamp |
| updated_at | TEXT | Last modification timestamp |

Unique constraint on `(session_key, name)`.

## YAML Format

Tools accept and return routines in YAML:

```yaml
name: morning-tech-news
schedule: "0 8 * * 1-5"
prompt: |
  Gather tech news and make a short "whats new" post.
enabled: true
```

## Tools

All three tools are scoped to the current session — they derive `session_key` from the dispatch context.

**`read_routine(name=None)`** — With `name`, returns that routine's YAML. Without `name`, returns a list of all routines for the session (name, schedule, enabled status).

**`write_routine(yaml_str)`** — Creates or updates a routine. Parses YAML, validates the cron expression via `cron.validate_cron_expression()`, computes `next_run_at` via `cron.next_cron_occurrence()`, and upserts into the DB. Returns the stored YAML.

**`delete_routine(name)`** — Removes a routine. Returns confirmation.

## Scheduler

A new `RoutineSchedulerTask` registered in the `HeartbeatRunner`. Runs on each heartbeat cycle (~60s):

1. Query all routines where `enabled = 1 AND next_run_at <= now()`
2. For each due routine:
   - Add the prompt as a user message via `SessionService.add_message()`
   - Fire-and-forget an independent asyncio task that dispatches to the LLM via `HarnessService` — no `SessionDispatchGate` lock acquired, so the routine does not block or serialize with other session activity
   - The response is stored in session history but not delivered to the user's channel
   - Compute `next_run_at` using `cron.next_cron_occurrence()`
   - Update `last_run_at` and `next_run_at`

## Migration

Single SQL migration file adding the `routines` table.

## Files to Create/Modify

- `schemas/XX_routines.sql` — Migration creating the `routines` table
- `services/routine_service.py` — DB CRUD for routines
- `services/routine_scheduler.py` — `RoutineSchedulerTask` heartbeat task
- `services/routine_tools.py` — `read_routine`, `write_routine`, `delete_routine` tool definitions
- `tool_registry.py` — Register routine tools
- `main.py` — Register `RoutineSchedulerTask` in heartbeat runner
