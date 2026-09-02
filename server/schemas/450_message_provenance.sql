-- Message provenance: distinguishes internal bookkeeping rows from real
-- dialogue so history replay can exclude them. NULL = normal dialogue.
-- Values: extraction_marker | dream_announcement | wake_nudge | routine.
-- (The `synthetic` column means "assistant reply that used memory recall
-- tools" and is deliberately NOT touched — see session_service.add_message.)
ALTER TABLE session_messages ADD COLUMN provenance TEXT;

-- Backfill by content/metadata pattern.
UPDATE session_messages SET provenance = 'extraction_marker'
 WHERE json_extract(metadata, '$.memory_extraction_turn') = 1
    OR content LIKE '[Silent extraction turn:%';

UPDATE session_messages SET provenance = 'dream_announcement'
 WHERE provenance IS NULL
   AND json_extract(metadata, '$.dream_announce') IS NOT NULL;

UPDATE session_messages SET provenance = 'routine'
 WHERE provenance IS NULL AND channel = 'routine';

CREATE INDEX IF NOT EXISTS idx_session_messages_provenance
    ON session_messages (session_key, provenance)
    WHERE provenance IS NOT NULL;
