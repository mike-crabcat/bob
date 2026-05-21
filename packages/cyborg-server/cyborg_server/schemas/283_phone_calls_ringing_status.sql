-- Add 'ringing' and 'canceled' to the phone_calls status CHECK constraint.
-- Also fix foreign key in phone_call_exchanges that points to _phone_calls_old.
-- SQLite doesn't support ALTER COLUMN, so we recreate both tables.

ALTER TABLE phone_calls RENAME TO _phone_calls_old;

CREATE TABLE phone_calls (
    id TEXT PRIMARY KEY,
    call_sid TEXT,
    stream_sid TEXT,
    phone_number TEXT,
    direction TEXT NOT NULL DEFAULT 'outbound'
        CHECK (direction IN ('inbound', 'outbound')),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('ringing', 'active', 'completed', 'failed', 'canceled', 'busy', 'no-answer')),
    agenda TEXT NOT NULL DEFAULT '',
    exchange_count INTEGER NOT NULL DEFAULT 0,
    duration_seconds REAL,
    recording_path TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO phone_calls SELECT * FROM _phone_calls_old;

-- Recreate phone_call_exchanges to fix foreign key reference
ALTER TABLE phone_call_exchanges RENAME TO _phone_call_exchanges_old;

CREATE TABLE phone_call_exchanges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL REFERENCES phone_calls(id),
    exchange_index INTEGER NOT NULL,
    user_transcript TEXT NOT NULL DEFAULT '',
    assistant_transcript TEXT NOT NULL DEFAULT '',
    stt_ms INTEGER,
    openclaw_ms INTEGER,
    tts_first_chunk_ms INTEGER,
    e2e_ms INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    gateway_prepare_ms INTEGER,
    gateway_stream_ms INTEGER,
    tts_wait_lock_ms INTEGER,
    tts_generate_ms INTEGER
);

INSERT INTO phone_call_exchanges SELECT * FROM _phone_call_exchanges_old;

DROP TABLE _phone_call_exchanges_old;
DROP TABLE _phone_calls_old;

CREATE INDEX IF NOT EXISTS idx_phone_calls_started
    ON phone_calls(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_phone_calls_call_sid
    ON phone_calls(call_sid);
CREATE INDEX IF NOT EXISTS idx_phone_call_exchanges_call
    ON phone_call_exchanges(call_id, exchange_index);
