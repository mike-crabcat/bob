CREATE TABLE IF NOT EXISTS session_participants (
    session_key TEXT NOT NULL,
    identifier TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    contact_id TEXT REFERENCES contacts(id),
    is_trusted INTEGER NOT NULL DEFAULT 0,
    last_active_at TEXT NOT NULL,
    PRIMARY KEY (session_key, identifier)
);
