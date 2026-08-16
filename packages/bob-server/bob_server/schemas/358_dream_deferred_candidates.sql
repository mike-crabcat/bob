-- Dream v2: deferred candidates. When a run hits max_new_items_per_type, the
-- capped candidate is persisted here (full JSON) instead of being dropped —
-- the next run processes deferred candidates FIRST, before new ones, so cap
-- pressure defers rather than discards. Evidence already behind a session
-- cursor would otherwise never re-propose.
CREATE TABLE dream_deferred_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_type TEXT NOT NULL CHECK (item_type IN ('resolution','plan')),
    session_key TEXT NOT NULL DEFAULT '',
    candidate_json TEXT NOT NULL,           -- full candidate incl. evidence
    created_at TEXT NOT NULL,
    source_run_id TEXT NOT NULL REFERENCES dream_runs(id)
);
CREATE INDEX idx_dream_deferred_type ON dream_deferred_candidates(item_type, id);
