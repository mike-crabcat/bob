CREATE TABLE IF NOT EXISTS session_agendas (
    session_key TEXT PRIMARY KEY,
    agenda TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
