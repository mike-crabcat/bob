-- Runs (commitments plan Phase 0, 2026-10-05): one execution record per
-- executor run, separate from intent. Detached flights (backburner) write a
-- run instead of a subagents row + kind='subagent' goal — those were ~90% of
-- the goals table and polluted goals_block with goal instructions that made
-- no sense for a flight. Physical plumbing tables (bg_jobs, phone_calls)
-- stay; later phases point their executors here too.
--
-- kind: flight | agent | job | call | turn
-- status: running | completed | failed | killed
-- metadata_json: kind-specific (flight: {"steer_origin": bool, "summary"})

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    session_key TEXT NOT NULL,          -- conversation the run serves
    dispatch_id TEXT,
    commitment_id TEXT,                 -- goal/commitment it serves (later phases)
    summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    result TEXT,
    error_message TEXT,
    metadata_json TEXT,
    external_ref TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_key, status);
CREATE INDEX IF NOT EXISTS idx_runs_kind ON runs(kind, status);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);

-- Backfill history: detached turns become flight runs; retired script
-- subagents become job runs. Ids are preserved so [bg id8] attribution and
-- old transcript placeholders still resolve via check_subagent.
INSERT OR IGNORE INTO runs (id, kind, session_key, summary, status, result,
                            error_message, started_at, ended_at)
SELECT id,
       CASE agent_type WHEN 'detached_turn' THEN 'flight' ELSE 'job' END,
       parent_session_key,
       task,
       CASE status WHEN 'completed' THEN 'completed'
                   WHEN 'killed' THEN 'killed'
                   WHEN 'running' THEN 'running'
                   ELSE 'failed' END,
       result, error_message, created_at,
       CASE WHEN status IN ('completed', 'failed', 'killed') THEN updated_at END
FROM subagents
WHERE agent_type IN ('detached_turn', 'script');
