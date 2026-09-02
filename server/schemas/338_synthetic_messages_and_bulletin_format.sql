-- Synthetic flag on session_messages and format marker on memory_bulletins.
--
-- session_messages.synthetic:
--   1 = assistant message generated during a dispatch that used memory-read
--       tools (recall / find / memory_read). These are echoes of existing
--       memory, not new ground truth, and the extraction LLM is instructed
--       to skip them.
--   0 = ordinary message (default).
ALTER TABLE session_messages ADD COLUMN synthetic INTEGER NOT NULL DEFAULT 0;

-- memory_bulletins.format:
--   'raw_transcript' = bulletin content is a literal transcript (new format).
--   'llm_summary'    = bulletin content is an LLM-generated summary (legacy).
-- Defaults to 'llm_summary' so pre-existing rows are labelled correctly
-- without backfill.
ALTER TABLE memory_bulletins ADD COLUMN format TEXT NOT NULL DEFAULT 'llm_summary';

-- Index to support fetching the last N messages for a session in descending
-- time order (used when building raw-transcript bulletins).
CREATE INDEX IF NOT EXISTS idx_session_messages_session_time_desc
    ON session_messages(session_key, created_at DESC);
