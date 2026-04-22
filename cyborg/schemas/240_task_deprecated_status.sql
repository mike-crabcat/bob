-- Add 'deprecated' task status for tasks obsoleted by spec revisions.
-- SQLite requires table rebuild to modify CHECK constraints.

PRAGMA foreign_keys = OFF;

DROP TABLE IF EXISTS tasks_v2;
CREATE TABLE tasks_v2 (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    requested_by TEXT,
    plan TEXT,
    status TEXT NOT NULL CHECK (status IN ('planning', 'pending', 'active', 'paused', 'blocked', 'submitted', 'completed', 'failed', 'deprecated')),
    blocked_reason TEXT,
    blocked_resume_instructions TEXT,
    blocked_at TEXT,
    priority TEXT NOT NULL CHECK (priority IN ('low', 'medium', 'high', 'critical')),
    parent_id TEXT REFERENCES tasks(id),
    current_plan_id TEXT REFERENCES plans(id),
    retry_config TEXT,
    is_recurring INTEGER NOT NULL DEFAULT 0,
    recurrence_rule TEXT,
    next_run_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    submitted_at TEXT,
    completed_at TEXT,
    result TEXT,
    metadata TEXT,
    notification_count INTEGER NOT NULL DEFAULT 0,
    last_notification_at TEXT,
    needs_input_since TEXT,
    deleted_at TEXT,
    submission_review_otp TEXT
);

INSERT INTO tasks_v2 (
    id, title, description, requested_by, plan, status,
    blocked_reason, blocked_resume_instructions, blocked_at,
    priority, parent_id, current_plan_id, retry_config,
    is_recurring, recurrence_rule, next_run_at,
    created_at, updated_at, started_at, submitted_at, completed_at, result,
    metadata, notification_count, last_notification_at, needs_input_since, deleted_at,
    submission_review_otp
)
SELECT
    id, title, description, requested_by, plan, status,
    blocked_reason, blocked_resume_instructions, blocked_at,
    priority, parent_id, current_plan_id, retry_config,
    is_recurring, recurrence_rule, next_run_at,
    created_at, updated_at, started_at, submitted_at, completed_at, result,
    metadata, notification_count, last_notification_at, needs_input_since, deleted_at,
    submission_review_otp
FROM tasks;

DROP TABLE tasks;
ALTER TABLE tasks_v2 RENAME TO tasks;

CREATE INDEX IF NOT EXISTS idx_tasks_parent_id ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_deleted_at ON tasks(deleted_at);

PRAGMA foreign_keys = ON;
