-- Voice chat session messages and lesson progress tracking.

CREATE TABLE IF NOT EXISTS voice_session_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
    text TEXT NOT NULL,
    language TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_voice_session_lookup
    ON voice_session_messages(session_key, created_at);

CREATE TABLE IF NOT EXISTS voice_lesson_progress (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    lesson_number INTEGER NOT NULL,
    step_index INTEGER NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    UNIQUE(user_id, mode, lesson_number, step_index)
);

CREATE TABLE IF NOT EXISTS voice_current_lesson (
    user_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    lesson_number INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (user_id, mode)
);
