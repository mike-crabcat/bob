-- Store harness dispatch logs with full prompt/response pairs
CREATE TABLE IF NOT EXISTS harness_logs (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    dispatch_id TEXT,
    session_key TEXT,
    model TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'openai',
    system_prompt TEXT NOT NULL DEFAULT '',
    user_message TEXT NOT NULL DEFAULT '',
    response TEXT NOT NULL DEFAULT '',
    history_messages INTEGER DEFAULT 0,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    cached_tokens INTEGER,
    latency_seconds REAL,
    ttft_seconds REAL,
    status TEXT NOT NULL DEFAULT 'completed' CHECK(status IN ('completed', 'failed', 'timeout')),
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_harness_logs_timestamp ON harness_logs(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_harness_logs_session ON harness_logs(session_key);
CREATE INDEX IF NOT EXISTS idx_harness_logs_model ON harness_logs(model);

-- Keep harness logs from growing forever
CREATE TRIGGER IF NOT EXISTS harness_logs_cleanup
AFTER INSERT ON harness_logs
BEGIN
    DELETE FROM harness_logs WHERE timestamp < datetime('now', '-30 days');
END;
