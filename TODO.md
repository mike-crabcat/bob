# Cleanup backlog

Tracked-but-not-scheduled cleanups. One section per item; check things off as they land.
Keep these decoupled from feature work — no cleanup should ride along on a feature branch.

## Legacy task/project/plan tables

The task execution engine (`task_service.py`, tasks/projects/planning routers,
`project_execution_service.py`, etc.) was removed in commit `8c3d016` (2026-05-22).
The tables it used are still in the database, orphaned. Last data write was
2026-05-02 — everything in them is historical.

**Tables to drop** (row counts as of 2026-08-16):

| Table | Rows | Remaining code refs |
|---|---|---|
| `tasks` | 33 | `session_route_service.py:419` (read-only) |
| `plans` | 0 | none |
| `task_history` | 164 | none |
| `task_files` | 116 | none |
| `task_steps` | 0 | none |
| `projects` | 10 | `session_route_service.py:414` (read-only) |
| `project_tasks` | 33 | `session_route_service.py:429` (read-only) |
| `project_specs` | 16 | none |
| `project_sources` | 7 | none |
| `project_journal_entries` | 184 | none |
| `project_health_checks` | 0 | none |
| `latest_project_health` | 0 | none |
| `project_insights` | 0 | none |
| `projects_need_attention` | 0 | none |

`self_executing_projects` (schema 50) was already dropped; the migration remains as history.

**Steps:**

- [ ] Optional: one-time JSON export of the non-empty tables to `~/data/` before dropping,
      in case the history is ever wanted (`tasks`, `task_history`, `task_files`, `projects`,
      `project_tasks`, `project_specs`, `project_sources`, `project_journal_entries`)
- [ ] Drop migration `358_drop_legacy_task_project_tables.sql` covering the 14 tables above
      (plain `DROP TABLE IF EXISTS`, matching the style of `353_drop_bulletin_dream_tables.sql`;
      renumbered from 357 — the dream-v2 tables migration took 357)
- [ ] Remove the vestigial task/project lookup in `session_route_service.py` (~lines 405–440):
      the `task_id` metadata branch and the `project_tasks` join. Routing falls through to the
      remaining metadata paths (`channel`, `chat_id`, `project_id` in metadata can go too if
      the projects branch above it is only reachable from dropped tables — verify before removing)
- [ ] Verify: server starts, migration applies cleanly, session routing still resolves for
      email/WhatsApp metadata (no task_id path), dashboard pages unaffected

**Out of scope:** the `task` *entity type* in the memory claim registry (`claim_types.py`)
is part of the memory system, not this engine — leave it alone.
