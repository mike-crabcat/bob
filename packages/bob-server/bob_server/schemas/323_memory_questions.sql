-- Memory reconciliation questions for human-in-the-loop conflict resolution.
CREATE TABLE IF NOT EXISTS memory_questions (
    id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL,
    question TEXT NOT NULL,
    options TEXT,          -- JSON array of suggested answers
    context TEXT,          -- what conflict triggered this question
    status TEXT NOT NULL DEFAULT 'open',  -- open, answered, dismissed
    answer TEXT,
    answer_claim_id TEXT,
    created_at TEXT NOT NULL,
    answered_at TEXT
);
