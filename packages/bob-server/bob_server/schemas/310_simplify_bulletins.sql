-- Simplify bulletins: drop entity tracking and structured metadata.
-- Memory is being cleared and re-seeded, so this is destructive.

-- Drop the bulletin entities table entirely
DROP TABLE IF EXISTS memory_bulletin_entities;

-- Recreate bulletins table with minimal columns
DROP TABLE IF EXISTS memory_bulletins;

CREATE TABLE memory_bulletins (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    channel_id TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL DEFAULT '',
    visibility TEXT NOT NULL DEFAULT 'channel'
        CHECK(visibility IN ('private','contact','group','channel','public')),
    content TEXT NOT NULL DEFAULT '',
    digested INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_memory_bulletins_created
    ON memory_bulletins(created_at DESC);
CREATE INDEX idx_memory_bulletins_channel
    ON memory_bulletins(channel_id);
CREATE INDEX idx_memory_bulletins_digested
    ON memory_bulletins(digested, created_at DESC);

-- Clear all derived memory tables
DELETE FROM memory_claims;
DELETE FROM memory_entities;
DELETE FROM memory_entity_relations;
DELETE FROM memory_aliases;
