-- Project insights for learning from completed projects
-- Stores lessons learned, patterns, and recommendations extracted from projects

CREATE TABLE IF NOT EXISTS project_insights (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    outcome_type TEXT NOT NULL CHECK(outcome_type IN ('success', 'failure', 'partial')),

    -- Insight categorization
    insight_category TEXT NOT NULL CHECK(insight_category IN ('planning', 'execution', 'estimation', 'communication', 'technical', 'coordination')),

    -- The learned insight (JSON for flexibility)
    insight_data JSON NOT NULL,

    -- When this insight should be applied (JSON pattern matching)
    -- Examples: {"keywords": ["migration", "microservices"], "project_type": "rewrite"}
    applicability_pattern JSON,

    -- Metadata
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    extracted_by TEXT,  -- 'system' or user reference

    -- Relationships
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_project_insights_project ON project_insights(project_id);
CREATE INDEX IF NOT EXISTS idx_project_insights_category ON project_insights(insight_category);
CREATE INDEX IF NOT EXISTS idx_project_insights_outcome ON project_insights(outcome_type);
CREATE INDEX IF NOT EXISTS idx_project_insights_created ON project_insights(created_at DESC);

-- Enable full-text search on insight descriptions if needed
-- CREATE VIRTUAL TABLE project_insights_fts USING fts5(id, content);

-- View for active insights (from successful or partially successful projects)
CREATE VIEW IF NOT EXISTS active_insights AS
SELECT
    id,
    project_id,
    insight_category,
    insight_data,
    applicability_pattern,
    created_at
FROM project_insights
WHERE outcome_type IN ('success', 'partial')
ORDER BY created_at DESC;
