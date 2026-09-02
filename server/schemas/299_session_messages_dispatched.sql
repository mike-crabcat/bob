-- Add dispatched flag to session_messages.
-- 0 = user message not yet processed by an LLM dispatch
-- 1 = message already dispatched / not applicable
-- Default 1 so existing rows are treated as already dispatched.

ALTER TABLE session_messages ADD COLUMN dispatched INTEGER NOT NULL DEFAULT 1;

CREATE INDEX IF NOT EXISTS idx_session_messages_undispatched
    ON session_messages(session_key, dispatched) WHERE dispatched = 0;
