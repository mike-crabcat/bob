-- 008: agent-side MCP registration (2026-09-14).
-- Bob can now register MCP servers at a trusted contact's request: owner
-- requests register directly, other trusted contacts' requests park an
-- approval in the owner's DM (the approvals machinery, like steering).
-- SQLite can't ALTER a CHECK, so approvals gets the standard rebuild dance
-- (same columns, 'mcp_server' appended to the type allowlist); mcp_servers
-- learns created_by for the audit trail.
CREATE TABLE IF NOT EXISTS "approvals_008" (
    id TEXT PRIMARY KEY,
    approval_type TEXT NOT NULL CHECK(approval_type IN ('project_plan', 'strategy_refinement', 'task_creation', 'follow_up_tasks', 'purchase', 'group_send', 'conversation_steer', 'sensation_route', 'mcp_server')),
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
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO approvals_008 (id, approval_type, entity_id, title, description,
                           proposal_data, status, priority, requested_at,
                           requested_by, reviewed_at, reviewed_by,
                           review_notes, metadata, created_at)
SELECT id, approval_type, entity_id, title, description,
       proposal_data, status, priority, requested_at,
       requested_by, reviewed_at, reviewed_by,
       review_notes, metadata, created_at
FROM approvals;
DROP VIEW IF EXISTS pending_approvals;   -- blocks the table drop; rebuilt below
DROP TABLE approvals;
ALTER TABLE approvals_008 RENAME TO approvals;
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_approvals_type ON approvals(approval_type, status);
CREATE INDEX IF NOT EXISTS idx_approvals_entity ON approvals(entity_id, approval_type);
CREATE INDEX IF NOT EXISTS idx_approvals_requested_at ON approvals(requested_at DESC);
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

ALTER TABLE mcp_servers ADD COLUMN created_by TEXT NOT NULL DEFAULT '';
