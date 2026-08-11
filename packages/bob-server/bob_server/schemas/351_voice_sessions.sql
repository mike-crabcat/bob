-- Bob-initiated browser voice sessions (no telco).
-- A voice_session is created when Bob offers the user a voice call from a chat
-- (WhatsApp). The token in the URL is a capability — anyone with it can join
-- the session as Bob until it completes. See services/voice_session_service.py.
CREATE TABLE voice_sessions (
    id TEXT PRIMARY KEY,
    origin_session_key TEXT NOT NULL,
    voice TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | active | completed | expired
    transcript TEXT NOT NULL DEFAULT '',
    duration_seconds REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    activated_at TEXT,
    completed_at TEXT
);
