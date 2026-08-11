-- Drop deprecated bulletin/dream tables. Silent-mode extraction is the only
-- memory path; these tables have not been written to since 2026-06-20.

DROP TABLE IF EXISTS memory_claim_bulletins;
DROP TABLE IF EXISTS memory_entity_bulletins;
DROP TABLE IF EXISTS memory_bulletin_entities;
DROP TABLE IF EXISTS memory_bulletins;
DROP TABLE IF EXISTS memory_dream_log;

-- memory_claims.source_bulletins referenced bulletin IDs that no longer exist.
ALTER TABLE memory_claims DROP COLUMN source_bulletins;
