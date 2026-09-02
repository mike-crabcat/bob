-- Approvals table for tracking items requiring user review and approval
-- This supports the approval workflow in the dashboard

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    approval_type TEXT NOT NULL CHECK(approval_type IN ('project_plan', 'strategy_refinement', 'task_creation', 'follow_up_tasks')),
    entity_id TEXT NOT NULL,  -- ID of the thing needing approval (project_id, task_id, etc.)
    title TEXT NOT NULL,
    description TEXT,
    proposal_data TEXT,  -- JSON string containing the full proposal
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected', 'cancelled')),
    priority TEXT DEFAULT 'normal' CHECK(priority IN ('low', 'normal', 'high', 'urgent')),
    requested_at TEXT NOT NULL,
    requested_by TEXT,  -- User or system that requested approval
    reviewed_at TEXT,
    reviewed_by TEXT,
    review_notes TEXT,
    metadata TEXT,  -- Additional JSON data
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_approvals_type ON approvals(approval_type, status);
CREATE INDEX IF NOT EXISTS idx_approvals_entity ON approvals(entity_id, approval_type);
CREATE INDEX IF NOT EXISTS idx_approvals_requested_at ON approvals(requested_at DESC);

-- View for pending approvals
CREATE VIEW IF NOT EXISTS pending_approvals AS
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
