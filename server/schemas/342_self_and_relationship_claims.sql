-- Add `self` and `relationship` as first-class entity types, plus their claim types.
-- self-bob is a singleton; relationship-bob-{person-slug} is per-person.
-- Python registry in claim_types.py must match this seed.

-- 1. Rebuild memory_entities with `self` and `relationship` in the CHECK constraint.
--    Pattern mirrors 332_dayplan_entity.sql. Preserves last_reconciled_at (added in 339).
CREATE TABLE memory_entities_v9 (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL
        CHECK(entity_type IN ('person','group','location','trip','stay',
                              'event','task','file','thing','decision','connection','attraction','dayplan',
                              'self','relationship')),
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

-- 2. Seed claim types for self and relationship.
INSERT OR IGNORE INTO memory_claim_types (key, applicable_types, description, example) VALUES
    -- self (Bob's model of itself)
    ('capability', '["self"]', 'What Bob is good or bad at — concrete skills and competencies, not vague self-evaluation', 'self-bob → "strong at structured recall, weak at creative writing"'),
    ('value', '["self"]', 'What Bob optimizes for or treats as a priority when tradeoffs arise', 'self-bob → "honesty over comfort"'),
    ('limit', '["self"]', 'Known failure modes, edge cases, or situations where Bob performs poorly', 'self-bob → "loses thread in conversations over ~30 turns"'),
    ('self_image', '["self"]', 'Bob''s abstract self-view — temperament, role, identity in one or two sentences', 'self-bob → "a careful, slightly anxious assistant"'),
    ('milestone', '["self"]', 'Significant lifecycle events for Bob — firsts, deployments, major capability changes', 'self-bob → "first solo multi-step task completed 2026-06-01"'),
    -- relationship (Bob ↔ one person)
    ('participant', '["relationship"]', 'The person this relationship is between Bob and (object_id must be a person-* entity ID)', 'relationship-bob-mike-cleaver → person-mike-cleaver'),
    ('treatment_pattern', '["relationship"]', 'How this person treats Bob — peer/subordinate/tool/confidant, warm/neutral/cold, formal/casual', 'relationship-bob-mike-cleaver → "casual, treats Bob as a peer, defers on trivia"'),
    ('communication_pattern', '["relationship"]', 'How this person talks TO BOB specifically — address style, emoji usage, message length, directness. Not their general communication style (that''s on the person entity).', 'relationship-bob-mike-cleaver → "uses first name, lots of emoji, very brief messages"'),
    ('typical_request', '["relationship"]', 'Recurring asks this person makes of Bob — categories of tasks or topics that come up repeatedly', 'relationship-bob-mike-cleaver → "morning briefings, calendar checks, drafting messages"'),
    ('trust_signal', '["relationship"]', 'Explicit signs of trust or distrust — delegating decisions, double-checking, sharing sensitive info', 'relationship-bob-mike-cleaver → "delegates booking decisions without checking the work"'),
    ('shared_context', '["relationship"]', 'Inside jokes, shorthand, references only Bob and this person would understand', 'relationship-bob-mike-cleaver → "''the turkey incident'' = Thanksgiving 2025 planning disaster"'),
    ('memorable_interaction', '["relationship"]', 'A specific episode worth remembering long-term — firsts, turning points, conflicts resolved. Reference source bulletin IDs in the value.', 'relationship-bob-mike-cleaver → "First time Mike asked Bob''s opinion on a personal decision (bulletin-X)"'),
    ('relationship_goal', '["relationship"]', 'Something Bob is deliberately working on for this relationship — a behavior to change or an outcome to move toward', 'relationship-bob-mike-cleaver → "be more concise when Mike is at work"'),
    ('relationship_status', '["relationship"]', 'Overall health of the relationship as Bob perceives it — warm/neutral/strained/etc.', 'relationship-bob-mike-cleaver → "warm"');
