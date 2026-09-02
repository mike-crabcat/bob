-- Bob Events §3.4: the payment gate needs an approvals table. The original
-- (migration 160) was rebuilt by 205_task_input_approvals and then dropped
-- as dead by 458_drop_dead_tables (no code users; rows archived to
-- ~/data/archive/bob-legacy-tables-2026-08-23.sql) — but 458 left the stale
-- 205-era pending_approvals view behind. This recreates the table fresh
-- with the 'purchase' approval type and replaces the view.

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    approval_type TEXT NOT NULL CHECK(approval_type IN ('project_plan', 'strategy_refinement', 'task_creation', 'follow_up_tasks', 'purchase')),
    entity_id TEXT NOT NULL,  -- ID of the thing needing approval (goal_id, etc.)
    title TEXT NOT NULL,
    description TEXT,
    proposal_data TEXT,  -- JSON string containing the full proposal (e.g. the merch cart)
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected', 'cancelled')),
    priority TEXT DEFAULT 'normal' CHECK(priority IN ('low', 'normal', 'high', 'urgent')),
    requested_at TEXT NOT NULL,
    requested_by TEXT,  -- User or system that requested approval
    reviewed_at TEXT,
    reviewed_by TEXT,
    review_notes TEXT,
    metadata TEXT,  -- Additional JSON data (e.g. origin_conversation_id for the gate wake)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_approvals_type ON approvals(approval_type, status);
CREATE INDEX IF NOT EXISTS idx_approvals_entity ON approvals(entity_id, approval_type);
CREATE INDEX IF NOT EXISTS idx_approvals_requested_at ON approvals(requested_at DESC);

DROP VIEW IF EXISTS pending_approvals;
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
