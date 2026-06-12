-- Add 'deprecated' to allowed memory_entities status values.
-- Used when a file entity loses its file_path or is otherwise invalidated.

CREATE TABLE memory_entities_v4 (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL
        CHECK(entity_type IN ('person','group','location','trip','tripstop',
                              'transport','event','task','file','thing','decision')),
    display_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','archived','deprecated')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO memory_entities_v4 (entity_id, entity_type, display_name, status, created_at, updated_at)
    SELECT entity_id, entity_type, display_name, status, created_at, updated_at
    FROM memory_entities;

DROP TABLE memory_entities;
ALTER TABLE memory_entities_v4 RENAME TO memory_entities;

CREATE INDEX IF NOT EXISTS idx_memory_entities_type
    ON memory_entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_memory_entities_display_name
    ON memory_entities(display_name);
