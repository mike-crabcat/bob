-- Dead-table cleanup: drop the pre-Bob3 task/project system, the frozen
-- dispatch substrate, and empty/orphaned legacy tables. None have code
-- readers or writers (verified 2026-08-23); last data writes were May 2026.
--
-- All rows archived first to ~/data/archive/bob-legacy-tables-2026-08-23.sql
-- (plain SQL dump, 1,359 rows) — restore by piping that file into sqlite3.

-- email_threads carried a never-used FK into projects; drop it before the
-- parent table goes (always NULL in prod, reader removed from routers/email.py).
DROP INDEX IF EXISTS idx_email_threads_project;
ALTER TABLE email_threads DROP COLUMN project_id;

-- Old task system (children before parents).
DROP TABLE IF EXISTS task_steps;
DROP TABLE IF EXISTS task_files;
DROP TABLE IF EXISTS task_history;
DROP TABLE IF EXISTS dispatches;
DROP TABLE IF EXISTS tasks;
DROP TABLE IF EXISTS plans;
DROP TABLE IF EXISTS notifications;
DROP TABLE IF EXISTS approvals;
DROP TABLE IF EXISTS subscriptions;

-- Old projects system.
DROP TABLE IF EXISTS project_tasks;
DROP TABLE IF EXISTS project_specs;
DROP TABLE IF EXISTS project_sources;
DROP TABLE IF EXISTS project_journal_entries;
DROP TABLE IF EXISTS project_health_checks;
DROP TABLE IF EXISTS project_insights;
DROP TABLE IF EXISTS projects;

-- Orphans: writers deleted or never wired.
DROP TABLE IF EXISTS prompt_history;
DROP TABLE IF EXISTS harness_logs;
DROP TABLE IF EXISTS persona_config;

-- Empty legacy voice/phone remnants (local STT->TTS pipeline).
DROP TABLE IF EXISTS voice_current_lesson;
DROP TABLE IF EXISTS voice_lesson_progress;
DROP TABLE IF EXISTS phone_call_exchanges;
