-- Composite index for contact_id claim lookups.
-- The contact -> entity lookup is `WHERE claim_type_key='contact_id'
-- AND value=? AND status='active'`. The existing idx_memory_claims_type
-- only indexed claim_type_key, so this query scanned every contact_id
-- claim. See services/memory/service.py:find_person_entry and
-- sync_person_display_name_for_contact for callers.

CREATE INDEX IF NOT EXISTS idx_memory_claims_type_value_status
    ON memory_claims(claim_type_key, value, status);
