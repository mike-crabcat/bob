-- Unified session messages — replaces voice_session_messages.
-- Stores conversation history for all channels: voice, phone, email, whatsapp.

CREATE TABLE IF NOT EXISTS session_messages (
    id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system', 'tool')),
    content TEXT NOT NULL,
    sender_id TEXT,
    channel TEXT,
    metadata TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_session_messages_key_time ON session_messages(session_key, created_at);
CREATE INDEX IF NOT EXISTS idx_session_messages_channel ON session_messages(channel);
