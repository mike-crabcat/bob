-- Join tables for fast bulletin↔entity and bulletin↔claim lookups.
-- Complements the JSON source_bulletins arrays with proper indexed relations.

CREATE TABLE memory_entity_bulletins (
    entity_id TEXT NOT NULL,
    bulletin_id TEXT NOT NULL,
    PRIMARY KEY (entity_id, bulletin_id)
);
CREATE INDEX idx_mem_ent_bulletins_entity ON memory_entity_bulletins(entity_id);
CREATE INDEX idx_mem_ent_bulletins_bulletin ON memory_entity_bulletins(bulletin_id);

CREATE TABLE memory_claim_bulletins (
    claim_id TEXT NOT NULL,
    bulletin_id TEXT NOT NULL,
    PRIMARY KEY (claim_id, bulletin_id)
);
CREATE INDEX idx_mem_claim_bulletins_claim ON memory_claim_bulletins(claim_id);
CREATE INDEX idx_mem_claim_bulletins_bulletin ON memory_claim_bulletins(bulletin_id);
