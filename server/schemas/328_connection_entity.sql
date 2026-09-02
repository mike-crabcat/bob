-- Add connection as a first-class entity type.
-- Updates CHECK constraint, adds connection-specific claim types,
-- converts the connection claim from value-based to entity reference.

-- 1. Rebuild memory_entities with 'connection' in CHECK constraint
CREATE TABLE memory_entities_v6 (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL
        CHECK(entity_type IN ('person','group','location','trip','stay',
                              'event','task','file','thing','decision','connection')),
    display_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','archived','deprecated')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO memory_entities_v6 SELECT * FROM memory_entities;
DROP TABLE memory_entities;
ALTER TABLE memory_entities_v6 RENAME TO memory_entities;

CREATE INDEX IF NOT EXISTS idx_memory_entities_type
    ON memory_entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_memory_entities_display_name
    ON memory_entities(display_name);

-- 2. Update the connection claim type from value-based to entity reference
UPDATE memory_claim_types SET
    description = 'A Connection entity that is part of this trip',
    example = 'trip-europe-france → connection-perth-geneva-outbound'
WHERE key = 'connection';

-- 3. Add connection-specific claim types
INSERT OR IGNORE INTO memory_claim_types (key, applicable_types, description, example) VALUES
    ('departure_location', '["connection"]', 'Where the journey starts (location or city name)', 'connection-perth-geneva → "Perth PER T1"'),
    ('arrival_location', '["connection"]', 'Where the journey ends (location or city name)', 'connection-perth-geneva → "Geneva GVA T1"'),
    ('departure_time', '["connection"]', 'Departure date/time', 'connection-perth-geneva → "2026-06-22T15:50"'),
    ('arrival_time', '["connection"]', 'Arrival date/time', 'connection-perth-geneva → "2026-06-23T10:50"'),
    ('transport_type', '["connection"]', 'Mode: flight, train, bus, ferry, car, taxi, other', 'connection-perth-geneva → "flight"'),
    ('duration', '["connection"]', 'Journey duration', 'connection-perth-geneva → "25h"'),
    ('booking_ref', '["connection"]', 'Booking reference, PNR, or confirmation code', 'connection-perth-geneva → "EPBT7N"'),
    ('route', '["connection"]', 'Route details: flight numbers, train numbers, intermediate stops', 'connection-perth-geneva → "MH124 PER→KUL, MH002 KUL→LHR, BA744 LHR→GVA"'),
    ('passenger', '["connection"]', 'Person traveling on this connection', 'connection-perth-geneva → person-mike-cleaver'),
    ('seat', '["connection"]', 'Seat or cabin assignment', 'connection-perth-geneva → "Coach 10, Seat 32"');

-- 4. Supersede old flat-text connection claims (rebuild will re-extract as entities)
UPDATE memory_claims
SET status = 'superseded', superseded_by = '["328_connection_entity"]'
WHERE claim_type_key = 'connection'
  AND value IS NOT NULL
  AND status = 'active';
