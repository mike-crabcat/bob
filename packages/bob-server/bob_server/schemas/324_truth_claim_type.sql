-- Add truth claim type for user corrections and answered questions.
-- Applies to all entity types.

INSERT OR IGNORE INTO memory_claim_types (key, applicable_types, description, example) VALUES (
    'truth',
    '["person","group","location","trip","tripstop","transport","event","task","file","thing","decision"]',
    'User-stated fact, correction, or answer. Ground truth from the user that overrides inference.',
    'trip-mike-holiday-june-2026 → "Yes, split Paris into two stops"'
);

-- Migrate old answer claims from purpose to truth
UPDATE memory_claims SET claim_type_key = 'truth' WHERE claim_type_key = 'purpose' AND value LIKE '[Q:%' AND status = 'active';
