-- Migration: Add plan versioning system for tasks
-- Creates the plans table and migrates existing task.plan data

-- Create the plans table
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    version_number INTEGER NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft', 'pending_approval', 'approved', 'rejected')),
    feedback TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    approved_by TEXT,
    is_current INTEGER NOT NULL DEFAULT 0,
    UNIQUE(task_id, version_number)
);

-- Index for efficient lookups
CREATE INDEX IF NOT EXISTS idx_plans_task_id ON plans(task_id);
CREATE INDEX IF NOT EXISTS idx_plans_task_id_current ON plans(task_id, is_current);
CREATE INDEX IF NOT EXISTS idx_plans_status ON plans(status);

-- Add current_plan_id to tasks table
ALTER TABLE tasks ADD COLUMN current_plan_id TEXT REFERENCES plans(id);

-- Migrate existing task.plan data to plans table
-- Create version 1 plans for all tasks that have a plan
INSERT INTO plans (id, task_id, version_number, content, status, created_at, approved_at, approved_by, is_current)
SELECT 
    lower(hex(randomblob(16))),
    t.id,
    1,
    t.plan,
    'approved',
    t.created_at,
    t.created_at,
    'system_migration',
    1
FROM tasks t
WHERE t.plan IS NOT NULL AND t.plan != '';

-- Update tasks to set current_plan_id to the newly created plan
UPDATE tasks
SET current_plan_id = (
    SELECT p.id 
    FROM plans p 
    WHERE p.task_id = tasks.id AND p.is_current = 1
)
WHERE plan IS NOT NULL AND plan != '';

-- Note: We keep the old plan column for backward compatibility during transition
-- A future migration can remove it once all code is updated
