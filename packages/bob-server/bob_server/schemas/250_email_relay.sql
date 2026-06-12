-- Email relay: AgentMail inbox registry, message dedup, thread tracking.

CREATE TABLE IF NOT EXISTS email_inboxes (
    id TEXT PRIMARY KEY,
    agentmail_inbox_id TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    email_address TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    last_polled_at TEXT,
    metadata TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_email_inboxes_agentmail_id ON email_inboxes(agentmail_inbox_id);
CREATE INDEX IF NOT EXISTS idx_email_inboxes_active ON email_inboxes(is_active);
CREATE INDEX IF NOT EXISTS idx_email_inboxes_deleted_at ON email_inboxes(deleted_at);

CREATE TABLE IF NOT EXISTS email_messages (
    id TEXT PRIMARY KEY,
    inbox_id TEXT NOT NULL REFERENCES email_inboxes(id),
    agentmail_message_id TEXT NOT NULL UNIQUE,
    thread_id TEXT NOT NULL,
    subject TEXT,
    sender_email TEXT NOT NULL,
    sender_name TEXT,
    to_addresses TEXT,
    cc_addresses TEXT,
    text_body TEXT,
    html_body TEXT,
    preview TEXT,
    labels TEXT,
    has_attachments INTEGER NOT NULL DEFAULT 0,
    in_reply_to TEXT,
    message_timestamp TEXT NOT NULL,
    processed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_email_messages_inbox ON email_messages(inbox_id);
CREATE INDEX IF NOT EXISTS idx_email_messages_thread ON email_messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_email_messages_agentmail_id ON email_messages(agentmail_message_id);
CREATE INDEX IF NOT EXISTS idx_email_messages_sender ON email_messages(sender_email);
CREATE INDEX IF NOT EXISTS idx_email_messages_timestamp ON email_messages(message_timestamp);

CREATE TABLE IF NOT EXISTS email_threads (
    id TEXT PRIMARY KEY,
    inbox_id TEXT NOT NULL REFERENCES email_inboxes(id),
    agentmail_thread_id TEXT NOT NULL,
    subject TEXT,
    contact_id TEXT REFERENCES contacts(id),
    project_id TEXT REFERENCES projects(id),
    session_key TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    last_message_at TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    metadata TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    UNIQUE(inbox_id, agentmail_thread_id)
);

CREATE INDEX IF NOT EXISTS idx_email_threads_inbox ON email_threads(inbox_id);
CREATE INDEX IF NOT EXISTS idx_email_threads_contact ON email_threads(contact_id);
CREATE INDEX IF NOT EXISTS idx_email_threads_project ON email_threads(project_id);
CREATE INDEX IF NOT EXISTS idx_email_threads_session_key ON email_threads(session_key);
CREATE INDEX IF NOT EXISTS idx_email_threads_active ON email_threads(is_active);
CREATE INDEX IF NOT EXISTS idx_email_threads_deleted_at ON email_threads(deleted_at);

-- Widen session_routes CHECK constraints to support email channel and thread kind.
-- SQLite requires table rebuild for CHECK constraint changes.

CREATE TABLE IF NOT EXISTS session_routes_new (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL CHECK (channel IN ('whatsapp', 'email')),
    session_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('group', 'dm', 'thread')),
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
    )
);

INSERT OR IGNORE INTO session_routes_new SELECT * FROM session_routes;

DROP TABLE IF EXISTS session_routes;

ALTER TABLE session_routes_new RENAME TO session_routes;

CREATE INDEX IF NOT EXISTS idx_session_routes_channel_session_key ON session_routes(channel, session_key);
CREATE INDEX IF NOT EXISTS idx_session_routes_active ON session_routes(is_active);
CREATE INDEX IF NOT EXISTS idx_session_routes_deleted_at ON session_routes(deleted_at);
