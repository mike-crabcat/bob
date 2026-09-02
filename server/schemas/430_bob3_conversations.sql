-- Bob3 Phase VI (items 1-2): conversations & bindings + mechanical 1:1
-- backfill.
--
-- Backfilled conversation ids equal the legacy session_key, so
-- event_log.conversation_id (which has carried session_keys since Phase I)
-- lines up without rewriting history. Merges move bindings onto a survivor
-- conversation; merged_from records provenance so unmerge can return
-- pre-merge events to their original conversation (via binding_key).

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'dm',         -- dm|group|internal
    title TEXT,
    policy_json TEXT,
    merged_into TEXT,                        -- set when this conversation was merged away
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bindings (
    session_key TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    channel TEXT NOT NULL,                   -- whatsapp|email|subagent|internal|voice
    kind TEXT NOT NULL DEFAULT 'thread',     -- identity|thread
    address TEXT,                            -- e.g. email address, phone number
    sensitivity TEXT,
    merged_from TEXT,                        -- original conversation_id before merge
    merged_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bindings_conversation
    ON bindings (conversation_id);

-- 1:1 backfill: one conversation per known session_key.
INSERT OR IGNORE INTO conversations (id, kind, created_at, updated_at)
SELECT sk,
       CASE
           WHEN sk LIKE '%:group:%' THEN 'group'
           WHEN sk LIKE '%:dm:%' THEN 'dm'
           ELSE 'internal'
       END,
       datetime('now'), datetime('now')
FROM (
    SELECT session_key AS sk FROM session_routes WHERE deleted_at IS NULL
    UNION
    SELECT DISTINCT session_key FROM session_messages
);

INSERT OR IGNORE INTO bindings (session_key, conversation_id, channel, kind, created_at)
SELECT id, id,
       CASE
           WHEN id LIKE '%:whatsapp:%' THEN 'whatsapp'
           WHEN id LIKE '%:email:%' THEN 'email'
           WHEN id LIKE 'subagent:%' THEN 'subagent'
           WHEN id LIKE '%:voice%' THEN 'voice'
           ELSE 'internal'
       END,
       'thread', datetime('now')
FROM conversations;
