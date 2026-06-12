CREATE TABLE IF NOT EXISTS memory_dream_log (
    id TEXT PRIMARY KEY,
    bulletins_processed INTEGER NOT NULL DEFAULT 0,
    entries_created INTEGER NOT NULL DEFAULT 0,
    bulletin_slugs TEXT NOT NULL DEFAULT '[]',
    operations_json TEXT NOT NULL DEFAULT '[]',
    raw_response TEXT,
    duration_seconds REAL,
    status TEXT NOT NULL DEFAULT 'completed',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_memory_dream_log_created ON memory_dream_log(created_at DESC);
