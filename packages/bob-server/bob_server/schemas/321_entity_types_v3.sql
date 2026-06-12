-- Entity types v3: contact→person, artifact→file+thing.
-- Also renames artifact_ref→file_ref and adds thing_type + contact_id claim types.

-- 1. Update entity_type CHECK on memory_entities via table swap.
CREATE TABLE memory_entities_v3 (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL
        CHECK(entity_type IN ('person','group','location','trip','tripstop',
                              'transport','event','task','file','thing','decision')),
    display_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','archived')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO memory_entities_v3 (entity_id, entity_type, display_name, status, created_at, updated_at)
SELECT entity_id,
    CASE
        WHEN entity_type = 'contact' THEN 'person'
        WHEN entity_type = 'artifact' THEN 'file'
        ELSE entity_type
    END,
    display_name, status, created_at, updated_at
FROM memory_entities;

DROP TABLE memory_entities;
ALTER TABLE memory_entities_v3 RENAME TO memory_entities;

CREATE INDEX IF NOT EXISTS idx_memory_entities_type
    ON memory_entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_memory_entities_display_name
    ON memory_entities(display_name);

-- 2. Update claim type applicable_types: contact→person, artifact→file/thing
UPDATE memory_claim_types SET applicable_types = REPLACE(applicable_types, '"contact"', '"person"') WHERE applicable_types LIKE '%"contact"%';
UPDATE memory_claim_types SET applicable_types = REPLACE(applicable_types, '"artifact"', '"file"') WHERE key IN ('file_path');
UPDATE memory_claim_types SET applicable_types = REPLACE(applicable_types, '"artifact"', '"file","thing"') WHERE key IN ('owner', 'name', 'purpose', 'related_entity');
UPDATE memory_claim_types SET applicable_types = REPLACE(applicable_types, '"artifact"', '"file","thing"') WHERE key = 'artifact_ref';

-- 3. Rename artifact_ref → file_ref
UPDATE memory_claim_types SET key = 'file_ref', description = 'Relevant file or document attached to this entity', example = 'trip-bali-2026 → file-villa-spreadsheet' WHERE key = 'artifact_ref';

-- 4. Update existing claims: artifact_ref → file_ref
UPDATE memory_claims SET claim_type_key = 'file_ref' WHERE claim_type_key = 'artifact_ref';

-- 5. Add new claim types
INSERT OR IGNORE INTO memory_claim_types (key, applicable_types, description, example) VALUES ('thing_type', '["thing"]', 'Kind of physical thing: animal, toy, tool, vehicle, furniture, appliance, food, device', 'thing-ebike → vehicle');
INSERT OR IGNORE INTO memory_claim_types (key, applicable_types, description, example) VALUES ('contact_id', '["person"]', 'Links a person entity to their contacts table row (value = hex8 ID from contacts table)', 'person-mike-cleaver → 7c9f0fd7');

-- 6. Update purpose to include file and thing
UPDATE memory_claim_types SET applicable_types = '["group","event","trip","file","thing","task"]' WHERE key = 'purpose';
