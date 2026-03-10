CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    aim TEXT,
    state TEXT NOT NULL CHECK (state IN ('planning', 'active', 'paused', 'closed')),
    created_at TEXT NOT NULL,
    started_at TEXT,
    paused_at TEXT,
    closed_at TEXT,
    conclusion TEXT,
    deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_projects_state ON projects(state);
CREATE INDEX IF NOT EXISTS idx_projects_deleted_at ON projects(deleted_at);

CREATE TABLE IF NOT EXISTS project_journal_entries (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    entry_type TEXT NOT NULL CHECK (entry_type IN ('note', 'milestone', 'decision', 'blocker', 'result')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    metadata TEXT
);

CREATE INDEX IF NOT EXISTS idx_project_journal_project_id ON project_journal_entries(project_id);

CREATE TABLE IF NOT EXISTS project_tasks (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (project_id, task_id)
);

CREATE INDEX IF NOT EXISTS idx_project_tasks_task_id ON project_tasks(task_id);
