-- Migration: Add planning task state and enforce approved-plan gating
-- Existing pending tasks without an approved current plan are moved to planning.

PRAGMA foreign_keys = OFF;

CREATE TABLE tasks_new (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    requested_by TEXT,
    plan TEXT,
    status TEXT NOT NULL CHECK (status IN ('planning', 'pending', 'active', 'paused', 'blocked', 'completed', 'failed')),
    blocked_reason TEXT,
    blocked_resume_instructions TEXT,
    blocked_at TEXT,
    priority TEXT NOT NULL CHECK (priority IN ('low', 'medium', 'high', 'critical')),
    parent_id TEXT REFERENCES tasks(id),
    retry_config TEXT,
    is_recurring INTEGER NOT NULL DEFAULT 0,
    recurrence_rule TEXT,
    next_run_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    metadata TEXT,
    deleted_at TEXT,
    current_plan_id TEXT REFERENCES plans(id)
);

INSERT INTO tasks_new (
    id, title, description, requested_by, plan, status,
    blocked_reason, blocked_resume_instructions, blocked_at,
    priority, parent_id, retry_config, is_recurring, recurrence_rule, next_run_at,
    created_at, updated_at, started_at, completed_at, metadata, deleted_at, current_plan_id
)
SELECT
    t.id,
    t.title,
    t.description,
    t.requested_by,
    t.plan,
    CASE
        WHEN t.status = 'pending' AND NOT EXISTS (
            SELECT 1
            FROM plans AS p
            WHERE p.id = t.current_plan_id AND p.status = 'approved'
        ) THEN 'planning'
        ELSE t.status
    END,
    t.blocked_reason,
    t.blocked_resume_instructions,
    t.blocked_at,
    t.priority,
    t.parent_id,
    t.retry_config,
    t.is_recurring,
    t.recurrence_rule,
    t.next_run_at,
    t.created_at,
    t.updated_at,
    t.started_at,
    t.completed_at,
    t.metadata,
    t.deleted_at,
    CASE
        WHEN t.status = 'pending' AND NOT EXISTS (
            SELECT 1
            FROM plans AS p
            WHERE p.id = t.current_plan_id AND p.status = 'approved'
        ) THEN NULL
        ELSE t.current_plan_id
    END
FROM tasks AS t;

DROP TABLE tasks;

ALTER TABLE tasks_new RENAME TO tasks;

CREATE INDEX IF NOT EXISTS idx_tasks_parent_id ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_deleted_at ON tasks(deleted_at);

PRAGMA foreign_keys = ON;
