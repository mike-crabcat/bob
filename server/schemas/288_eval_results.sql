CREATE TABLE IF NOT EXISTS eval_runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    category TEXT,
    total_cases INTEGER NOT NULL DEFAULT 0,
    passed_cases INTEGER NOT NULL DEFAULT 0,
    failed_cases INTEGER NOT NULL DEFAULT 0,
    overall_pass_rate REAL,
    status TEXT NOT NULL DEFAULT 'running'
);

CREATE INDEX IF NOT EXISTS idx_eval_runs_started ON eval_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS eval_case_results (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES eval_runs(id),
    case_id TEXT NOT NULL,
    category TEXT NOT NULL,
    passed INTEGER NOT NULL DEFAULT 0,
    llm_response TEXT NOT NULL DEFAULT '',
    llm_latency_seconds REAL,
    judge_score REAL,
    judge_reasoning TEXT,
    structural_results_json TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_eval_case_results_run ON eval_case_results(run_id);
CREATE INDEX IF NOT EXISTS idx_eval_case_results_case ON eval_case_results(case_id, created_at DESC);
