-- Subagent tracking — generic async subagent system replacing skill-specific delegation
CREATE TABLE IF NOT EXISTS subagents (
    id TEXT PRIMARY KEY,
    parent_session_key TEXT NOT NULL,
    session_key TEXT NOT NULL UNIQUE,
    task TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'created',
    result TEXT,
    error_message TEXT,
    agent_type TEXT NOT NULL DEFAULT 'claude',
    claude_session_id TEXT,
    cost_usd REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_subagents_parent
    ON subagents(parent_session_key, status);
