-- Approval-gated cross-group WhatsApp messages: a per-message human gate for
-- sending to a group Bob is a member of. Adds the 'group_send' approval type.
-- SQLite cannot ALTER a CHECK constraint, so the table is rebuilt (the 460 /
-- 195 recreate pattern). NOTE: 'group_send' approvals route their gate wake
-- to the owner's DM, not to requested_by — see services/group_send_approval.py.
--
-- The three 150/140-era project views below were left dangling by 458's
-- table drops; the ALTER ... RENAME in this migration is the first DDL since
-- that forces SQLite to re-parse every view, so they are dropped here to
-- complete 458's cleanup (nothing reads them).

DROP VIEW IF EXISTS active_insights;
DROP VIEW IF EXISTS latest_project_health;
DROP VIEW IF EXISTS projects_need_attention;

DROP VIEW IF EXISTS pending_approvals;

DROP TABLE IF EXISTS approvals_v2;
CREATE TABLE approvals_v2 (
    id TEXT PRIMARY KEY,
    approval_type TEXT NOT NULL CHECK(approval_type IN ('project_plan', 'strategy_refinement', 'task_creation', 'follow_up_tasks', 'purchase', 'group_send')),
    entity_id TEXT NOT NULL,  -- ID of the thing needing approval (goal_id, etc.)
    title TEXT NOT NULL,
    description TEXT,
    proposal_data TEXT,  -- JSON string containing the full proposal (e.g. the merch cart, or a group message)
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
