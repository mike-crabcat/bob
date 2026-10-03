-- LLM trace events (2026-10-03 trace uplift): durable per-round timeline for
-- LLM turns — reasoning summaries/raw thinking, tool calls and results with
-- per-round latency, written incrementally while a turn runs. One llm_call_log
-- row per dispatch stays the metrics/attribution record; this table is the
-- replayable story. Payload redaction at 30d aligns with llm_call_log
-- (LlmTraceRetention sweep); 90d hard delete rides the llm_call_log_cleanup
-- trigger via ON DELETE CASCADE (foreign_keys pragma is on).
--
-- Also: reasoning_effort on llm_call_log (the chosen effort was never
-- recorded), and messages.dispatch_id so history rows join to their turn's
-- calls/effects directly instead of by timestamp window.

CREATE TABLE IF NOT EXISTS llm_trace_events (
    id TEXT PRIMARY KEY,
    llm_call_id TEXT NOT NULL REFERENCES llm_call_log(id) ON DELETE CASCADE,
    dispatch_id TEXT,
    session_key TEXT,
    iteration INTEGER NOT NULL DEFAULT 0,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,           -- round_started|reasoning_part|tool_call|tool_result|round_completed|turn_note
    content TEXT NOT NULL DEFAULT '',
    meta_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_llm_trace_call ON llm_trace_events(llm_call_id, seq);
CREATE INDEX IF NOT EXISTS idx_llm_trace_dispatch ON llm_trace_events(dispatch_id);

ALTER TABLE llm_call_log ADD COLUMN reasoning_effort TEXT;
ALTER TABLE messages ADD COLUMN dispatch_id TEXT;
