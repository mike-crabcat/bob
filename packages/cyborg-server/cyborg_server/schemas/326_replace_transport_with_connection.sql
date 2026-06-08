-- Replace transport entity type with connection claims on trips.
-- Archives all transport entities and supersedes their claims.
-- Removes transport-specific claim types from the registry.

-- Add connection claim type
INSERT OR IGNORE INTO memory_claim_types (key, applicable_types, description, example)
VALUES (
    'connection',
    '["trip"]',
    'A transport/connection leg within the trip. Self-contained text describing mode, route, date, times, booking details.',
    'trip-europe-france -> "Outbound Perth -> Geneva via KL and London, 22 Jun 2026, MH124+MH002+BA744, 25h, ref EPBT7N"'
);

-- Remove transport-specific claim types
DELETE FROM memory_claim_types WHERE key IN (
    'transport_to', 'transport_from', 'transport_type',
    'departure_time', 'duration', 'departure_location', 'arrival_location'
);

-- Supersede transport_to/transport_from claims on tripstops
UPDATE memory_claims
SET status = 'superseded', superseded_by = '["326_transport_removal"]'
WHERE claim_type_key IN ('transport_to', 'transport_from')
  AND status = 'active';

-- Supersede all claims on transport entities
UPDATE memory_claims
SET status = 'superseded', superseded_by = '["326_transport_removal"]'
WHERE subject_id IN (
    SELECT entity_id FROM memory_entities WHERE entity_type = 'transport'
) AND status = 'active';

-- Archive transport entities
UPDATE memory_entities
SET status = 'archived'
WHERE entity_type = 'transport' AND status = 'active';
