-- Commitments plan cleanup (2026-10-06): promises live in goals
-- (kind='promise', migration 018); the old tasks table was frozen since.
-- Archived to ~/data/archive/bob-tasks-table-2026-10-06.sql (171 rows).
DROP INDEX IF EXISTS idx_tasks_waiter;
DROP INDEX IF EXISTS idx_tasks_completer;
DROP INDEX IF EXISTS idx_tasks_due;
DROP TABLE IF EXISTS tasks;
