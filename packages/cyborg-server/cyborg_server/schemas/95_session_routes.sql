-- Registry mapping logical session keys to concrete outbound delivery targets.

CREATE TABLE IF NOT EXISTS session_routes (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL CHECK (channel IN ('whatsapp')),
    session_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('group', 'dm')),
    chat_id TEXT,
    contact_id TEXT REFERENCES contacts(id),
    metadata TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    UNIQUE(channel, session_key),
    CHECK (
        (kind = 'group' AND chat_id IS NOT NULL AND contact_id IS NULL)
        OR
        (kind = 'dm' AND contact_id IS NOT NULL AND chat_id IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_session_routes_channel_session_key ON session_routes(channel, session_key);
CREATE INDEX IF NOT EXISTS idx_session_routes_active ON session_routes(is_active);
CREATE INDEX IF NOT EXISTS idx_session_routes_deleted_at ON session_routes(deleted_at);
