-- Store structured logs for dashboard viewing
-- Logs are written here in addition to file/stdout for querying

CREATE TABLE IF NOT EXISTS structured_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    level TEXT NOT NULL CHECK(level IN ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')),
    logger TEXT NOT NULL,
    message TEXT NOT NULL,
    module TEXT,
    function TEXT,
    line INTEGER,
    event_type TEXT,
    project_id TEXT,
    duration_seconds REAL,
    extra_data JSON,
    correlation_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON structured_logs(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_logs_level ON structured_logs(level);
CREATE INDEX IF NOT EXISTS idx_logs_event_type ON structured_logs(event_type);
CREATE INDEX IF NOT EXISTS idx_logs_project ON structured_logs(project_id);
CREATE INDEX IF NOT EXISTS idx_logs_correlation ON structured_logs(correlation_id);

-- Keep logs table from growing forever - delete logs older than 30 days
CREATE TRIGGER IF NOT EXISTS structured_logs_cleanup
AFTER INSERT ON structured_logs
BEGIN
    DELETE FROM structured_logs WHERE timestamp < datetime('now', '-30 days');
END;
