-- Increment 6b: session_messages -> messages, keyed by canonical
-- conversation_id (merge-coherent history). binding_key preserves the exact
-- endpoint the message rode (same convention as event_log.binding_key).
-- conversation_id is a soft FK: subagent/internal keys write messages without
-- requiring a conversations row (conversations.id == legacy session_key 1:1).

CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    binding_key TEXT,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system', 'tool')),
    content TEXT NOT NULL,
    sender_id TEXT,
    channel TEXT,
    metadata TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    dispatched INTEGER NOT NULL DEFAULT 1,
    synthetic INTEGER NOT NULL DEFAULT 0,
    tool_summary TEXT,
    tool_blocks_json TEXT,
    provenance TEXT
);

-- ORDER BY rowid keeps insertion order so rowid tiebreaks (second-granularity
-- created_at) survive the copy.
INSERT INTO messages (id, conversation_id, binding_key, role, content, sender_id,
                      channel, metadata, created_at, dispatched, synthetic,
                      tool_summary, tool_blocks_json, provenance)
SELECT sm.id, COALESCE(b.conversation_id, sm.session_key), sm.session_key,
       sm.role, sm.content, sm.sender_id, sm.channel, sm.metadata, sm.created_at,
       sm.dispatched, sm.synthetic, sm.tool_summary, sm.tool_blocks_json, sm.provenance
FROM session_messages sm
LEFT JOIN bindings b ON b.session_key = sm.session_key
ORDER BY sm.rowid;

DROP TABLE session_messages;

CREATE INDEX idx_messages_conv_time ON messages(conversation_id, created_at);
CREATE INDEX idx_messages_conv_time_desc ON messages(conversation_id, created_at DESC);
CREATE INDEX idx_messages_channel ON messages(channel);
CREATE INDEX idx_messages_undispatched
    ON messages(conversation_id, dispatched) WHERE dispatched = 0;
CREATE INDEX idx_messages_provenance
    ON messages(conversation_id, provenance) WHERE provenance IS NOT NULL;
