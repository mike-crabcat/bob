-- Migration: Fix task status CHECK constraint to include 'blocked'
-- Issue: The original schema added blocked columns but didn't update the CHECK constraint

-- SQLite doesn't support ALTER TABLE to modify CHECK constraints directly.
-- We need to recreate the table with the correct constraint.

-- Step 1: Create a new table with the correct constraint
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
    deleted_at TEXT
);

-- Step 2: Copy data from old table to new table
-- Explicitly list columns to handle schema differences
INSERT INTO tasks_new (
    id, title, description, requested_by, plan, status,
    blocked_reason, blocked_resume_instructions, blocked_at,
    priority, parent_id, retry_config, is_recurring, recurrence_rule, next_run_at,
    created_at, updated_at, started_at, completed_at, metadata, deleted_at
)
SELECT 
    id, title, description, requested_by, plan, status,
    blocked_reason, blocked_resume_instructions, NULL as blocked_at,
    priority, parent_id, retry_config, is_recurring, recurrence_rule, next_run_at,
    created_at, updated_at, started_at, completed_at, metadata, deleted_at
FROM tasks;

-- Step 3: Drop the old table
DROP TABLE tasks;

-- Step 4: Rename the new table to the original name
ALTER TABLE tasks_new RENAME TO tasks;

-- Step 5: Recreate indexes
CREATE INDEX idx_tasks_parent_id ON tasks(parent_id);
CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_deleted_at ON tasks(deleted_at);
