-- Utility conversations + route valves (docs/utility-conversations-plan.md):
-- headless agent-authored behaviors fed by the stimulus spine. Phase 1 ships
-- inert — no seed routes; the first client (arrival-herald) is created
-- through request_sensation_route + owner approval, per plan Part 6.

CREATE TABLE IF NOT EXISTS utility_conversations (
  session_key  TEXT PRIMARY KEY,          -- agent:<slug>:utility
  title        TEXT NOT NULL,
  charter      TEXT NOT NULL,             -- behaviour spec, injected per turn
  model_alias  TEXT NOT NULL DEFAULT 'cheap',
  enabled      INTEGER NOT NULL DEFAULT 1,
  created_by   TEXT NOT NULL,             -- requesting session
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);

-- Router-enforced valves on routes (plan Part 2). NULL = no valve: the
-- seeded routes (cryptobro, frigate→guard) keep byte-identical behavior —
-- their throttling stays source-side as today.
ALTER TABLE stimulus_routes ADD COLUMN hours TEXT;              -- 'HH:MM-HH:MM' local house time, NULL = 24h
ALTER TABLE stimulus_routes ADD COLUMN cooldown_s INTEGER;      -- min seconds between steers on this route
ALTER TABLE stimulus_routes ADD COLUMN budget_per_hour INTEGER; -- max steered events per rolling hour

-- Audit join: which route handled an event (first matching route id when
-- several match — fan-out deliveries are counted in stimulus_route_fires).
ALTER TABLE stimulus_events ADD COLUMN route_id INTEGER;

-- Per-route fire log: the valve state (cooldown last-fire, rolling budget)
-- reads off this, not off events, so multi-route events count for every
-- route that delivered them. Pruned with the same 30-day cutoff.
CREATE TABLE IF NOT EXISTS stimulus_route_fires (
  route_id INTEGER NOT NULL,
  ts       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stimulus_route_fires
  ON stimulus_route_fires(route_id, ts);

-- Approvals learns the 'sensation_route' type. SQLite can't ALTER a CHECK,
-- so this is the standard rebuild dance (same columns, extended allowlist).
CREATE TABLE IF NOT EXISTS "approvals_003" (
    id TEXT PRIMARY KEY,
    approval_type TEXT NOT NULL CHECK(approval_type IN ('project_plan', 'strategy_refinement', 'task_creation', 'follow_up_tasks', 'purchase', 'group_send', 'conversation_steer', 'sensation_route')),
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
INSERT INTO approvals_003 (id, approval_type, entity_id, title, description,
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
ALTER TABLE approvals_003 RENAME TO approvals;
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
