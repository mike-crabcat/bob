-- Silent-turn memory extraction: message-level provenance and turn tracking.
--
-- The silent-turn extractor writes claims as a tool-loop turn in the session
-- (a synthetic assistant message). Provenance for those claims points at the
-- turn's session_message id rather than a bulletin, parallel to source_bulletins.
--
-- memory_extraction_turns records each silent turn so idle detection can find
-- "messages since the last turn" — the silent-mode analogue of how the bulletin
-- path uses memory_bulletins.session_range_end.

ALTER TABLE memory_claims ADD COLUMN source_messages TEXT NOT NULL DEFAULT '[]';

CREATE TABLE IF NOT EXISTS memory_extraction_turns (
    id             TEXT PRIMARY KEY,
    session_key    TEXT NOT NULL,
    message_id     TEXT NOT NULL,
    ran_at         TEXT NOT NULL,
    claims_created INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_memory_extraction_turns_session
    ON memory_extraction_turns(session_key, ran_at DESC);
