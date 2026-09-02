CREATE TABLE IF NOT EXISTS project_specs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version_number INTEGER NOT NULL,
    aim TEXT NOT NULL,
    method TEXT NOT NULL,
    plan TEXT,
    success_criteria TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending_approval', 'approved', 'rejected')),
    feedback TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    approved_by TEXT,
    is_current INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_project_specs_project_version
    ON project_specs(project_id, version_number);

CREATE INDEX IF NOT EXISTS idx_project_specs_project_id
    ON project_specs(project_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_project_specs_current
    ON project_specs(project_id)
    WHERE is_current = 1;

ALTER TABLE projects ADD COLUMN current_spec_id TEXT;

CREATE INDEX IF NOT EXISTS idx_projects_current_spec_id
    ON projects(current_spec_id);
