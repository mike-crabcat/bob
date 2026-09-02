-- Widen session_routes CHECK constraint to support phone channel.
-- SQLite requires table rebuild for CHECK constraint changes.

CREATE TABLE IF NOT EXISTS session_routes_new (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL CHECK (channel IN ('whatsapp', 'email', 'phone')),
    session_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('group', 'dm', 'thread', 'call')),
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
        OR
        (kind = 'thread' AND chat_id IS NOT NULL AND contact_id IS NULL)
        OR
        (kind = 'call' AND contact_id IS NOT NULL AND chat_id IS NULL)
    )
);

INSERT OR IGNORE INTO session_routes_new SELECT * FROM session_routes;

DROP TABLE IF EXISTS session_routes;

ALTER TABLE session_routes_new RENAME TO session_routes;

CREATE INDEX IF NOT EXISTS idx_session_routes_channel_session_key ON session_routes(channel, session_key);
CREATE INDEX IF NOT EXISTS idx_session_routes_active ON session_routes(is_active);
CREATE INDEX IF NOT EXISTS idx_session_routes_deleted_at ON session_routes(deleted_at);
