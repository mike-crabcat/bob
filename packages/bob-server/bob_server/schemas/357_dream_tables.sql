-- Dream v2: reflective self-improvement (resolutions) + proactive plans.
-- Full design in dream-v2-plan.md at the repo root. Dreams run on idle via the
-- heartbeat, review sessions since per-session cursors, and produce:
--   dream_resolutions — evidence-cited self-improvement items (kept when verified)
--   dream_plans       — unfinished business detected in conversation, announced in
--                       the session where the evidence was cited
-- Everything auditable via dream_runs.journal_text + stats_json.

CREATE TABLE dream_runs (
    id TEXT PRIMARY KEY,                    -- dream-YYYY-MM-DD-hex8
    started_at TEXT NOT NULL,
    finished_at TEXT,
    window_start TEXT NOT NULL,             -- descriptive only; coverage is per-session cursors
    window_end TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running','complete','failed')),
    trigger TEXT NOT NULL CHECK (trigger IN ('heartbeat','manual','cli')),
    model TEXT NOT NULL DEFAULT '',
    sessions_reviewed_json TEXT NOT NULL DEFAULT '[]',
    stats_json TEXT NOT NULL DEFAULT '{}',
    journal_text TEXT NOT NULL DEFAULT '',
    error TEXT
);

CREATE TABLE dream_resolutions (
    id TEXT PRIMARY KEY,                    -- resolution-hex8
    title TEXT NOT NULL,
    behaviour TEXT NOT NULL,                -- the observable behaviour, good or bad
    trigger_condition TEXT NOT NULL,        -- when/where it applies
    success_signal TEXT NOT NULL,           -- what a future dream checks to mark kept
    status TEXT NOT NULL CHECK (status IN
        ('draft','open','in_program','kept','dropped','stale')),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    observation_count INTEGER NOT NULL DEFAULT 1,
    evidence_json TEXT NOT NULL DEFAULT '[]', -- [{run_id, session_key, line, excerpt, kind}]
    source_run_id TEXT NOT NULL REFERENCES dream_runs(id),
    program_id TEXT                         -- reserved for future improvement programs
);

CREATE TABLE dream_plans (
    id TEXT PRIMARY KEY,                    -- plan-hex8
    title TEXT NOT NULL,
    what_was_discussed TEXT NOT NULL,
    proposed_action TEXT NOT NULL,          -- the concrete next step for the human(s)
    assistance_method TEXT NOT NULL,        -- how Bob can assist, grounded in real tools
    autonomy_tier INTEGER NOT NULL DEFAULT 1 CHECK (autonomy_tier IN (1,2)),
    status TEXT NOT NULL CHECK (status IN
        ('draft','proposed','approved','actioned','completed','expired','dismissed')),
    approved_by TEXT,                       -- 'operator' | 'auto' | NULL
    approved_at TEXT,
    announced_at TEXT,                      -- NULL until announced in its evidence session
    reannounced_at TEXT,                    -- set when the single follow-up is spent
    evidence_json TEXT NOT NULL DEFAULT '[]',
    source_run_id TEXT NOT NULL REFERENCES dream_runs(id),
    due_hint TEXT,                          -- free-text/date from conversation, if any
    task_id TEXT,                           -- reserved; no execution engine exists today
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE dream_item_links (
    item_type TEXT NOT NULL CHECK (item_type IN ('resolution','plan')),
    item_id TEXT NOT NULL,
    session_key TEXT,
    entity_id TEXT                          -- person-*/group-* memory entities
);
CREATE INDEX idx_dream_links_entity ON dream_item_links(entity_id, item_type);
CREATE INDEX idx_dream_links_session ON dream_item_links(session_key, item_type);
CREATE INDEX idx_dream_links_item ON dream_item_links(item_type, item_id);

-- Per-session review cursors (pattern of memory_extraction_turns): how far each
-- session has been reviewed. Sessions qualify for a dream when they have messages
-- newer than their cursor.
CREATE TABLE dream_session_review (
    session_key TEXT PRIMARY KEY,
    last_reviewed_message_at TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES dream_runs(id),
    updated_at TEXT NOT NULL
);

-- Runtime toggles settable without restart (slash command / dashboard).
-- Env settings are boot defaults; values here override.
CREATE TABLE dream_config (
    key TEXT PRIMARY KEY,                   -- e.g. 'auto_approve_plans'
    value TEXT NOT NULL,                    -- JSON-encoded
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_dream_resolutions_status ON dream_resolutions(status);
CREATE INDEX idx_dream_plans_status ON dream_plans(status);
CREATE INDEX idx_dream_runs_window ON dream_runs(window_end);
CREATE INDEX idx_dream_plans_pending_announce ON dream_plans(status, announced_at);

-- Embedding vectors for candidate dedup/merge against existing items.
CREATE VIRTUAL TABLE IF NOT EXISTS dream_item_embeddings
USING vec0(
    item_id TEXT PRIMARY KEY,
    embedding float[1536]
);
