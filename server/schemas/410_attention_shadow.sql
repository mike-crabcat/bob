-- Bob3 Phase III: Attention shadow mode.
-- Records the would-be decisions of the new Attention coordinator alongside
-- the live dispatcher, so cutover can be gated on measured agreement.
-- Telemetry only — never enters event_log; safe to prune.

CREATE TABLE IF NOT EXISTS attention_shadow (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT,                       -- event_log id of the stimulus (if appended)
    session_key TEXT NOT NULL,           -- conversation/binding key
    source TEXT NOT NULL,                -- whatsapp | email | phone | routine
    chat_kind TEXT,                      -- dm | group | thread
    addressed INTEGER NOT NULL,          -- Tier 0 structural decision
    addressed_reason TEXT,               -- dm | mention_jid | name_variant | reply_to_bot | not_addressed
    proposed_window_ms INTEGER NOT NULL, -- Tier 1 debounce the coordinator would use
    decision TEXT NOT NULL,              -- ACT | WAIT | STAND_DOWN (Tier 0/2 combined; shadow)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_attention_shadow_session
    ON attention_shadow(session_key, created_at);
CREATE INDEX IF NOT EXISTS idx_attention_shadow_created
    ON attention_shadow(created_at);
