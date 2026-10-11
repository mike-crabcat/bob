-- Curated self-model (docs/prompt-layout-plan.md Phase 2b, 2026-10-11).
-- One row per nightly self-curation dream stage: the structured output, its
-- rendered prompt block, the optional per-group social read, the
-- housekeeping it applied (retired self claims, how-to moves proposed), and
-- the one-line morning digest for Mike. The self-brief renders the newest
-- 'ready' row (< 48h) plus an in-day delta of key self claims written since.
CREATE TABLE IF NOT EXISTS dream_self_model (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    local_date TEXT NOT NULL,
    model TEXT,
    status TEXT NOT NULL,            -- ready | failed
    curated_json TEXT,
    rendered TEXT,
    social_rendered TEXT,
    housekeeping_json TEXT,
    source_counts_json TEXT,
    digest TEXT,
    digest_sent_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_dream_self_model_created
    ON dream_self_model(created_at);
