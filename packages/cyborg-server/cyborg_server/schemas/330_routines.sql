-- Routines: cron-scheduled prompts injected into sessions
CREATE TABLE IF NOT EXISTS routines (
    id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    name TEXT NOT NULL,
    schedule TEXT NOT NULL,
    prompt TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    next_run_at TEXT NOT NULL,
    last_run_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(session_key, name)
);
