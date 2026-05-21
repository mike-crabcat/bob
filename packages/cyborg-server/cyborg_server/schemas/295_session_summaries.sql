CREATE TABLE IF NOT EXISTS session_summaries (
    id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    active_from TEXT NOT NULL,
    active_to TEXT NOT NULL,
    summary_text TEXT NOT NULL DEFAULT '',
    topics TEXT NOT NULL DEFAULT '[]',
    participants TEXT NOT NULL DEFAULT '[]',
    memory_prompts TEXT NOT NULL DEFAULT '[]',
    message_count INTEGER NOT NULL DEFAULT 0,
    model_used TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_session_summaries_key
    ON session_summaries(session_key, active_to DESC);
