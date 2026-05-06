-- Phone call logging: call metadata and per-exchange transcript + latency.

CREATE TABLE IF NOT EXISTS phone_calls (
    id TEXT PRIMARY KEY,
    call_sid TEXT,
    stream_sid TEXT,
    phone_number TEXT,
    direction TEXT NOT NULL DEFAULT 'outbound'
        CHECK (direction IN ('inbound', 'outbound')),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'completed', 'failed')),
    agenda TEXT NOT NULL DEFAULT '',
    exchange_count INTEGER NOT NULL DEFAULT 0,
    duration_seconds REAL,
    recording_path TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_phone_calls_started
    ON phone_calls(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_phone_calls_call_sid
    ON phone_calls(call_sid);

CREATE TABLE IF NOT EXISTS phone_call_exchanges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL REFERENCES phone_calls(id),
    exchange_index INTEGER NOT NULL,
    user_transcript TEXT NOT NULL DEFAULT '',
    assistant_transcript TEXT NOT NULL DEFAULT '',
    stt_ms INTEGER,
    openclaw_ms INTEGER,
    tts_first_chunk_ms INTEGER,
    e2e_ms INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_phone_call_exchanges_call
    ON phone_call_exchanges(call_id, exchange_index);
