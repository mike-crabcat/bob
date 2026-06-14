-- Track when each entity was last reconciled, so the auto scheduler can
-- enforce a minimum interval between reconciliations of the same entity.
ALTER TABLE memory_entities ADD COLUMN last_reconciled_at TEXT;
