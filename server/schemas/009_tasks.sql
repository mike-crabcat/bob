-- Task registry (docs/task-registry-plan.md) — durable promises between
-- conversations. NOT an execution engine: a task holds a callback (waiter,
-- completer, result), never work state. Settling is CAS-once; delivery is
-- the task_settle effect (crash between settle and wake replays, never
-- loses); every task carries a due backstop wakeup.
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,                     -- task-<hex8>
    title TEXT NOT NULL,
    waiter_session TEXT NOT NULL,            -- conversation woken on settle
    payload_json TEXT NOT NULL DEFAULT '{}', -- context for the completer
    expected_completer TEXT,                 -- session/subagent id/unit/'script' (liveness hint, not a gate)
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','completed','failed','cancelled')),
    result TEXT,
    error TEXT,
    completed_by TEXT,                       -- provenance: who/what settled
    completed_at TEXT,
    delivered_at TEXT,                       -- waiter woken (exactly-once across effect replays)
    due TEXT NOT NULL,                       -- ISO; wakeup backstop
    source_goal_id TEXT,                     -- optional: registering room's goal
    refs_json TEXT NOT NULL DEFAULT '[]',    -- entity refs for completer-side extraction seeding
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_waiter ON tasks(waiter_session, status);
CREATE INDEX IF NOT EXISTS idx_tasks_completer ON tasks(expected_completer, status);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(status, due);
