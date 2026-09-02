-- Add person-specific preference claim types for richer personalisation.
-- Also updates the existing 'preference' claim type and adds 'communication_style'.

INSERT OR IGNORE INTO memory_claim_types (key, applicable_types, description, example) VALUES
    ('music_preference', '["person"]', 'Music tastes — genres, artists, instruments they play', 'person-mike-cleaver → "loves indie rock, plays guitar"'),
    ('sport_preference', '["person"]', 'Sports they follow or play — teams, leagues, athletes', 'person-mike-cleaver → "AFL Eagles fan, plays social tennis"'),
    ('entertainment_preference', '["person"]', 'Movies, TV shows, books, games, podcasts they enjoy', 'person-mike-cleaver → "sci-fi movies, Dark Souls, sci-fi novels"'),
    ('pet', '["person"]', 'Pets they have — type, name, breed', 'person-mike-cleaver → "golden retriever named Bella"'),
    ('communication_style', '["person"]', 'How they like to communicate — formal/casual, brief/detailed, emoji usage, banter level, directness', 'person-mike-cleaver → "casual, appreciates banter, hates corporate speak"');

-- Update existing 'preference' to be a clearer fallback
UPDATE memory_claim_types SET
    description = 'General preference not covered by a specific type (e.g. dark mode, morning person, early bird)',
    example = 'person-mike-cleaver → "prefers dark mode"'
WHERE key = 'preference';
