-- Unified LLM interaction log, replaces prompt_history and harness_logs
CREATE TABLE IF NOT EXISTS llm_call_log (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    provider TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    call_category TEXT NOT NULL,
    session_key TEXT,
    system_prompt TEXT NOT NULL DEFAULT '',
    user_message TEXT NOT NULL DEFAULT '',
    messages_json TEXT,
    response_text TEXT NOT NULL DEFAULT '',
    latency_seconds REAL,
    ttft_seconds REAL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    cached_tokens INTEGER,
    status TEXT NOT NULL DEFAULT 'completed',
    error_message TEXT,
    project_id TEXT,
    task_id TEXT,
    dispatch_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_llm_call_log_created ON llm_call_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_call_log_provider ON llm_call_log(provider, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_call_log_category ON llm_call_log(call_category, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_call_log_session ON llm_call_log(session_key);
CREATE INDEX IF NOT EXISTS idx_llm_call_log_dispatch ON llm_call_log(dispatch_id);
CREATE INDEX IF NOT EXISTS idx_llm_call_log_project ON llm_call_log(project_id);

CREATE TRIGGER IF NOT EXISTS llm_call_log_cleanup
AFTER INSERT ON llm_call_log
BEGIN
    DELETE FROM llm_call_log WHERE created_at < datetime('now', '-90 days');
END;
