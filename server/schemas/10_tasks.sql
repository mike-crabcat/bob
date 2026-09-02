CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    requested_by TEXT,
    plan TEXT,
    status TEXT NOT NULL CHECK (status IN ('planning', 'pending', 'active', 'paused', 'blocked', 'completed', 'failed')),
    blocked_reason TEXT,
    blocked_resume_instructions TEXT,
    priority TEXT NOT NULL CHECK (priority IN ('low', 'medium', 'high', 'critical')),
    parent_id TEXT REFERENCES tasks(id),
    retry_config TEXT,
    is_recurring INTEGER NOT NULL DEFAULT 0,
    recurrence_rule TEXT,
    next_run_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    metadata TEXT,
    deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_parent_id ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_deleted_at ON tasks(deleted_at);

CREATE TABLE IF NOT EXISTS task_steps (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    step_number INTEGER NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'completed', 'failed')),
    result TEXT,
    started_at TEXT,
    completed_at TEXT,
    UNIQUE(task_id, step_number)
);

CREATE INDEX IF NOT EXISTS idx_task_steps_task_id ON task_steps(task_id);

CREATE TABLE IF NOT EXISTS task_history (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    details TEXT,
    timestamp TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_history_task_id ON task_history(task_id);
