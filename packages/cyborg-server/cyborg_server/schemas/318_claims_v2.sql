-- Claims v2: claim_type_key FK, value replaces predicate, drop body.
-- Creates a new table with the correct schema and swaps it in.

-- Step 1: Create the new v2 table.
CREATE TABLE IF NOT EXISTS memory_claims_v2 (
    id TEXT PRIMARY KEY,
    claim_type_key TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    object_id TEXT,
    value TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','superseded','retracted','expired',
                         'disputed','archived','redundant','disproven','obsolete')),
    visibility TEXT NOT NULL DEFAULT 'channel'
        CHECK(visibility IN ('private','contact','group','channel','public')),
    scope TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    superseded_by TEXT NOT NULL DEFAULT '[]',
    source_bulletins TEXT NOT NULL DEFAULT '[]',
    CHECK((object_id IS NOT NULL) + (value IS NOT NULL AND value != '') <= 1),
    FOREIGN KEY (claim_type_key) REFERENCES memory_claim_types(key)
);

-- Step 2: Copy data from old table if v2 is empty.
-- Uses 'type' column (always present in pre-v2 schema).
INSERT OR IGNORE INTO memory_claims_v2 (id, claim_type_key, subject_id, object_id, value,
    status, visibility, scope, created_at, superseded_by, source_bulletins)
SELECT
    id,
    type,
    subject_id,
    object_id,
    CASE WHEN predicate != '' AND object_id IS NULL THEN predicate ELSE NULL END,
    status, visibility, scope, created_at, superseded_by, source_bulletins
FROM memory_claims
WHERE (SELECT COUNT(*) FROM memory_claims_v2) = 0;

-- Step 3: Swap tables.
DROP TABLE IF EXISTS memory_claims;
ALTER TABLE memory_claims_v2 RENAME TO memory_claims;

-- Step 4: Recreate indexes.
CREATE INDEX IF NOT EXISTS idx_memory_claims_subject
    ON memory_claims(subject_id);
CREATE INDEX IF NOT EXISTS idx_memory_claims_object
    ON memory_claims(object_id);
CREATE INDEX IF NOT EXISTS idx_memory_claims_status
    ON memory_claims(status);
CREATE INDEX IF NOT EXISTS idx_memory_claims_type
    ON memory_claims(claim_type_key);
