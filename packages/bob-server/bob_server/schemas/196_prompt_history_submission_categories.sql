-- Add submission_review and task_retry to prompt_history category CHECK constraint

DROP TABLE IF EXISTS prompt_history_v2;
CREATE TABLE prompt_history_v2 (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    category TEXT NOT NULL CHECK(category IN (
        'plan_generation',
        'criteria_evaluation',
        'strategy_refinement',
        'learning_extraction',
        'task_planning',
        'health_analysis',
        'follow_up_generation',
        'task_assignment',
        'needs_input',
        'notification',
        'submission_review',
        'task_retry'
    )),
    prompt_text TEXT NOT NULL,
    project_id TEXT,
    task_id TEXT,
    session_key TEXT,
    token_count_estimate INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO prompt_history_v2
    (id, timestamp, category, prompt_text, project_id, task_id, session_key, token_count_estimate, created_at)
SELECT
    id, timestamp, category, prompt_text, project_id, task_id, session_key, token_count_estimate, created_at
FROM prompt_history;

DROP TABLE IF EXISTS prompt_history;
ALTER TABLE prompt_history_v2 RENAME TO prompt_history;

CREATE INDEX IF NOT EXISTS idx_prompt_history_timestamp ON prompt_history(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_prompt_history_category ON prompt_history(category);
CREATE INDEX IF NOT EXISTS idx_prompt_history_project ON prompt_history(project_id);
CREATE INDEX IF NOT EXISTS idx_prompt_history_task ON prompt_history(task_id);
CREATE INDEX IF NOT EXISTS idx_prompt_history_session ON prompt_history(session_key);

CREATE TRIGGER IF NOT EXISTS prompt_history_cleanup
AFTER INSERT ON prompt_history
BEGIN
    DELETE FROM prompt_history WHERE timestamp < datetime('now', '-90 days');
END;
