-- Steering (docs/steering-plan.md): a steering request wakes the target
-- conversation to act on an instruction, replacing the verbatim cross-group
-- relay. Adds the 'conversation_steer' approval type; any still-pending
-- 'group_send' approvals are cancelled — the delivery hook is gone with
-- services/group_send_approval.py, so leaving them pending would wedge the
-- owner's approval list on requests nothing can execute.
-- SQLite cannot ALTER a CHECK constraint, so the table is rebuilt
-- (the 462 / 460 recreate pattern).

DROP VIEW IF EXISTS pending_approvals;

DROP TABLE IF EXISTS approvals_v2;
CREATE TABLE approvals_v2 (
    id TEXT PRIMARY KEY,
    approval_type TEXT NOT NULL CHECK(approval_type IN ('project_plan', 'strategy_refinement', 'task_creation', 'follow_up_tasks', 'purchase', 'group_send', 'conversation_steer')),
    entity_id TEXT NOT NULL,  -- ID of the thing needing approval (goal_id, etc.)
    title TEXT NOT NULL,
    description TEXT,
    proposal_data TEXT,  -- JSON string containing the full proposal (e.g. the merch cart, or a steering instruction + rendered wake)
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

INSERT INTO approvals_v2 (id, approval_type, entity_id, title, description,
                          proposal_data, status, priority, requested_at,
                          requested_by, reviewed_at, reviewed_by, review_notes,
                          metadata, created_at)
SELECT id, approval_type, entity_id, title, description,
       proposal_data, status, priority, requested_at,
       requested_by, reviewed_at, reviewed_by, review_notes,
       metadata, created_at
FROM approvals;

DROP TABLE approvals;
ALTER TABLE approvals_v2 RENAME TO approvals;

UPDATE approvals
SET status = 'cancelled',
    reviewed_at = datetime('now'),
    review_notes = COALESCE(review_notes || ' · ', '') ||
        'Retired 2026-08-30: the verbatim group relay was replaced by steering (docs/steering-plan.md)'
WHERE approval_type = 'group_send' AND status = 'pending';

CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_approvals_type ON approvals(approval_type, status);
CREATE INDEX IF NOT EXISTS idx_approvals_entity ON approvals(entity_id, approval_type);
CREATE INDEX IF NOT EXISTS idx_approvals_requested_at ON approvals(requested_at DESC);

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
