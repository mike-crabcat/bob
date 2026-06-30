-- Add daylog as a first-class entity type (retrospective counterpart to dayplan).
-- Updates CHECK constraint, adds daylog-scoped claim types (date, notes,
-- associated_trip extended; new media_ref + daylog claim on trip).

-- 1. Rebuild memory_entities with 'daylog' in CHECK constraint
DROP TABLE IF EXISTS memory_entities_v9;
CREATE TABLE memory_entities_v9 (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL
        CHECK(entity_type IN ('person','group','location','trip','stay',
                              'event','task','file','thing','decision','connection',
                              'attraction','dayplan','self','relationship','daylog')),
    display_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','archived','deprecated')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_reconciled_at TEXT
);

INSERT INTO memory_entities_v9 (entity_id, entity_type, display_name, status, created_at, updated_at, last_reconciled_at)
SELECT entity_id, entity_type, display_name, status, created_at, updated_at, last_reconciled_at
FROM memory_entities;
DROP TABLE memory_entities;
ALTER TABLE memory_entities_v9 RENAME TO memory_entities;

CREATE INDEX IF NOT EXISTS idx_memory_entities_type
    ON memory_entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_memory_entities_display_name
    ON memory_entities(display_name);

-- 2. Extend date, notes, associated_trip to also apply to daylog
UPDATE memory_claim_types
SET applicable_types = '["dayplan","daylog"]'
WHERE key = 'date';

UPDATE memory_claim_types
SET applicable_types = '["dayplan","daylog"]'
WHERE key = 'notes';

UPDATE memory_claim_types
SET applicable_types = REPLACE(applicable_types, ']', ',"daylog"]')
WHERE key = 'associated_trip'
  AND applicable_types NOT LIKE '%"daylog"%';

-- 3. Add media_ref claim type (workspace path or URL to a media file)
INSERT OR IGNORE INTO memory_claim_types (key, applicable_types, description, example) VALUES
    ('media_ref', '["daylog"]',
     'Workspace-relative path or URL to a media file (photo, video, audio) for this day',
     'daylog-bali-aug3 → "photos/bali-aug3/beach.jpg"');

-- 4. Add daylog claim type on trips
INSERT OR IGNORE INTO memory_claim_types (key, applicable_types, description, example) VALUES
    ('daylog', '["trip"]',
     'A Daylog entity — record of what happened on a specific day of the trip',
     'trip-bali-2026 → daylog-bali-aug3');
