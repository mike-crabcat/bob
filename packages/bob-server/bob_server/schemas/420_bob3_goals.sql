-- Bob3 Phase V: goals + transition history.
--
-- A goal is durable intent held by a conversation. conversation_id and
-- origin_conversation_id hold session_keys until Phase VI introduces
-- conversations (1:1 backfill planned).
--
-- Concurrency: status changes CAS on current status; revisions CAS on
-- version. Linked effects carry the goal's idempotency scope.

CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,           -- conversation working the goal
    origin_conversation_id TEXT,             -- conversation to wake on completion
    kind TEXT NOT NULL DEFAULT 'task',       -- task|outreach|subagent|call|email_thread
    objective TEXT NOT NULL,
    strategy_json TEXT,
    progress TEXT,
    result TEXT,
    status TEXT NOT NULL DEFAULT 'active',   -- active|completed|failed|cancelled
    version INTEGER NOT NULL DEFAULT 1,
    deadline TEXT,
    external_ref TEXT,                       -- e.g. subagent id, call id, thread id
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_goals_conversation
    ON goals (conversation_id, status);
CREATE INDEX IF NOT EXISTS idx_goals_origin
    ON goals (origin_conversation_id, status);
CREATE INDEX IF NOT EXISTS idx_goals_external_ref
    ON goals (external_ref);

CREATE TABLE IF NOT EXISTS goal_transitions (
    id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL REFERENCES goals(id),
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    version INTEGER NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_goal_transitions_goal
    ON goal_transitions (goal_id, created_at);
