-- Track files produced by tasks and stored in the project workspace.
-- Each row represents a file written to a task's output directory.

CREATE TABLE IF NOT EXISTS task_files (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK(purpose IN (
        'reasoning', 'result', 'analysis', 'log', 'artifact', 'other'
    )),
    description TEXT,
    content_type TEXT NOT NULL DEFAULT 'text/plain',
    size_bytes INTEGER,
    metadata TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),

    FOREIGN KEY (task_id) REFERENCES tasks(id),
    FOREIGN KEY (project_id) REFERENCES projects(id),
    UNIQUE(task_id, filename)
);

CREATE INDEX IF NOT EXISTS idx_task_files_task ON task_files(task_id);
CREATE INDEX IF NOT EXISTS idx_task_files_project ON task_files(project_id);
CREATE INDEX IF NOT EXISTS idx_task_files_purpose ON task_files(purpose);

-- Keep task_files from growing forever - delete rows older than 365 days
CREATE TRIGGER IF NOT EXISTS task_files_cleanup
AFTER INSERT ON task_files
BEGIN
    DELETE FROM task_files WHERE created_at < datetime('now', '-365 days');
END;
