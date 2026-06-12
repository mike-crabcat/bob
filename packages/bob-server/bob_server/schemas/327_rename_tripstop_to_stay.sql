-- Rename entity type "tripstop" → "stay"
-- Rename claim keys: stop→leg, stay→accommodation, arrival→arrival_date, departure→departure_date
-- Rename entity ID prefix "tripstop-" → "stay-"
-- Add new claim types: accommodation_type, accommodation_address

-- 1. Rebuild memory_entities with updated CHECK constraint
CREATE TABLE memory_entities_v5 (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL
        CHECK(entity_type IN ('person','group','location','trip','stay',
                              'event','task','file','thing','decision')),
    display_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','archived','deprecated')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO memory_entities_v5 (entity_id, entity_type, display_name, status, created_at, updated_at)
SELECT
    CASE WHEN entity_id LIKE 'tripstop-%' THEN 'stay-' || SUBSTR(entity_id, 10) ELSE entity_id END,
    CASE WHEN entity_type = 'tripstop' THEN 'stay' ELSE entity_type END,
    CASE WHEN display_name LIKE 'tripstop-%' THEN 'stay-' || SUBSTR(display_name, 10) ELSE display_name END,
    status, created_at, updated_at
FROM memory_entities;

DROP TABLE memory_entities;
ALTER TABLE memory_entities_v5 RENAME TO memory_entities;

CREATE INDEX IF NOT EXISTS idx_memory_entities_type
    ON memory_entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_memory_entities_display_name
    ON memory_entities(display_name);

-- 2. Rename entity ID references in claims (MUST happen before claim key rename)
UPDATE memory_claims SET subject_id = 'stay-' || SUBSTR(subject_id, 10)
    WHERE subject_id LIKE 'tripstop-%';
UPDATE memory_claims SET object_id = 'stay-' || SUBSTR(object_id, 10)
    WHERE object_id LIKE 'tripstop-%';

-- 3. Rename claim type keys (now IDs are stay-*, so filters work)
UPDATE memory_claims SET claim_type_key = 'accommodation'
    WHERE claim_type_key = 'stay' AND subject_id LIKE 'stay-%';
UPDATE memory_claims SET claim_type_key = 'arrival_date'
    WHERE claim_type_key = 'arrival' AND subject_id LIKE 'stay-%';
UPDATE memory_claims SET claim_type_key = 'departure_date'
    WHERE claim_type_key = 'departure' AND subject_id LIKE 'stay-%';
UPDATE memory_claims SET claim_type_key = 'leg'
    WHERE claim_type_key = 'stop';

-- 4. Update aliases (IDs already renamed above)
UPDATE memory_aliases SET entity_id = 'stay-' || SUBSTR(entity_id, 10)
    WHERE entity_id LIKE 'tripstop-%';

-- 5. Update entity-bulletin links
UPDATE memory_entity_bulletins SET entity_id = 'stay-' || SUBSTR(entity_id, 10)
    WHERE entity_id LIKE 'tripstop-%';

-- 6. Clear FTS for old tripstop IDs (app will rebuild)
DELETE FROM memory_entities_fts WHERE entity_id LIKE 'tripstop-%';

-- 7. Update claim type registry
UPDATE memory_claim_types SET key = 'accommodation', applicable_types = '["stay"]',
    description = 'Location/accommodation where you stay',
    example = 'stay-paris-june12-14 -> location-hotel-le-marais'
    WHERE key = 'stay';

UPDATE memory_claim_types SET key = 'arrival_date', applicable_types = '["stay"]',
    description = 'Date/time of arrival at this stay',
    example = 'stay-paris-june12-14 -> "2026-06-12T15:00"'
    WHERE key = 'arrival';

UPDATE memory_claim_types SET key = 'departure_date', applicable_types = '["stay"]',
    description = 'Date/time of departure from this stay',
    example = 'stay-paris-june14-14 -> "2026-06-14T10:00"'
    WHERE key = 'departure';

UPDATE memory_claim_types SET key = 'leg', description = 'A Stay that is part of this trip',
    example = 'trip-bali-2026 -> stay-bali-day1-3'
    WHERE key = 'stop';

-- Update cross-cutting claim types: tripstop -> stay
UPDATE memory_claim_types SET applicable_types = REPLACE(applicable_types, '"tripstop"', '"stay"')
    WHERE applicable_types LIKE '%"tripstop"%';

-- Add new claim types
INSERT OR IGNORE INTO memory_claim_types (key, applicable_types, description, example) VALUES (
    'accommodation_type', '["stay"]',
    'Type of accommodation: hotel, airbnb, hostel, camping, resort, apartment, villa',
    'stay-paris-june12-14 -> "hotel"'
);
INSERT OR IGNORE INTO memory_claim_types (key, applicable_types, description, example) VALUES (
    'accommodation_address', '["stay"]',
    'Street address of the accommodation',
    'stay-paris-june12-14 -> "12 Rue de Rivoli, Paris 75001"'
);
