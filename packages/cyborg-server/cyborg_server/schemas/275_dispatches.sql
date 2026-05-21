CREATE TABLE IF NOT EXISTS dispatches (
    id TEXT PRIMARY KEY,
    notification_id TEXT REFERENCES notifications(id),
    notification_type TEXT NOT NULL,
    session_key TEXT NOT NULL,
    task_id TEXT,
    project_id TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'completed', 'failed', 'timed_out', 'cancelled')),
    dispatched_at TEXT NOT NULL,
    completed_at TEXT,
    last_tapped_at TEXT,
    tap_count INTEGER NOT NULL DEFAULT 0,
    max_auto_taps INTEGER NOT NULL DEFAULT 0,
    duration_seconds REAL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dispatches_status ON dispatches(status, dispatched_at DESC);
CREATE INDEX IF NOT EXISTS idx_dispatches_session_key ON dispatches(session_key, status);
CREATE INDEX IF NOT EXISTS idx_dispatches_task_id ON dispatches(task_id, status);
CREATE INDEX IF NOT EXISTS idx_dispatches_notification_id ON dispatches(notification_id);
