-- Derived project relationships: a project can have 0+ source projects whose
-- outputs (venv, scripts, reports, etc.) are available for reuse.
CREATE TABLE IF NOT EXISTS project_sources (
    derived_project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    auto_discovered    INTEGER NOT NULL DEFAULT 0,
    relevance_score    REAL,
    relevance_reason   TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (derived_project_id, source_project_id)
);

CREATE INDEX IF NOT EXISTS idx_project_sources_derived
    ON project_sources(derived_project_id);
CREATE INDEX IF NOT EXISTS idx_project_sources_source
    ON project_sources(source_project_id);
