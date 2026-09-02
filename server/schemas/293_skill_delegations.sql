-- Skill delegation tracking
CREATE TABLE IF NOT EXISTS skill_delegations (
    id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    user_story TEXT NOT NULL,
    plan TEXT,
    claude_session_id TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    files_created_json TEXT,
    result_summary TEXT,
    cost_usd REAL,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
