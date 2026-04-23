-- Extend approvals to support structured task input requests

-- Add task_input to approval_type enum
-- SQLite doesn't support ALTER COLUMN, so we recreate the constraint via a new table.
-- However, since the original uses CHECK with IN (...), we can't simply add to it.
-- Instead, we drop and recreate the approvals table with the extended type list.

-- First, create a temporary backup
CREATE TABLE IF NOT EXISTS approvals_backup AS SELECT * FROM approvals;

DROP TABLE IF EXISTS approvals;
DROP VIEW IF EXISTS pending_approvals;

CREATE TABLE approvals (
    id TEXT PRIMARY KEY,
    approval_type TEXT NOT NULL CHECK(approval_type IN ('project_plan', 'strategy_refinement', 'task_creation', 'follow_up_tasks', 'task_input')),
    entity_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    proposal_data TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected', 'cancelled')),
    priority TEXT DEFAULT 'normal' CHECK(priority IN ('low', 'normal', 'high', 'urgent')),
    requested_at TEXT NOT NULL,
    requested_by TEXT,
    reviewed_at TEXT,
    reviewed_by TEXT,
    review_notes TEXT,
    metadata TEXT,
    input_schema TEXT,
    input_response TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Restore data from backup (columns that exist will be populated, new columns will be NULL)
INSERT INTO approvals (id, approval_type, entity_id, title, description, proposal_data, status, priority, requested_at, requested_by, reviewed_at, reviewed_by, review_notes, metadata, created_at)
SELECT id, approval_type, entity_id, title, description, proposal_data, status, priority, requested_at, requested_by, reviewed_at, reviewed_by, review_notes, metadata, created_at
FROM approvals_backup;

DROP TABLE approvals_backup;

-- Recreate indexes
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_approvals_type ON approvals(approval_type, status);
CREATE INDEX IF NOT EXISTS idx_approvals_entity ON approvals(entity_id, approval_type);
CREATE INDEX IF NOT EXISTS idx_approvals_requested_at ON approvals(requested_at DESC);

-- Recreate view for pending approvals
CREATE VIEW pending_approvals AS
SELECT
    id,
    approval_type,
    entity_id,
    title,
    description,
    proposal_data,
    priority,
    requested_at,
    requested_by,
    input_schema,
    created_at
FROM approvals
WHERE status = 'pending'
ORDER BY
    CASE priority
        WHEN 'urgent' THEN 1
        WHEN 'high' THEN 2
        WHEN 'normal' THEN 3
        WHEN 'low' THEN 4
    END,
    requested_at ASC;
