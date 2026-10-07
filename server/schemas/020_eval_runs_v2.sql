-- Eval runs v2 (docs/evals.md, 2026-10-08). Results now live in their own
-- store (~/data/evals.db, BOB_EVAL_DB) built from these same migrations;
-- worlds are throwaway DBs and the live bob.db is never opened by a run.
-- The model used to ride the category string ("all@gpt-6-luna"); every
-- comparability fact is a column now, and a case result is one TRIAL
-- (repeat_index) — pass rates and intervals are computed per case.
ALTER TABLE eval_runs ADD COLUMN model TEXT;
ALTER TABLE eval_runs ADD COLUMN provider_pin TEXT;
ALTER TABLE eval_runs ADD COLUMN effort TEXT;
ALTER TABLE eval_runs ADD COLUMN judge_model TEXT;
ALTER TABLE eval_runs ADD COLUMN git_sha TEXT;
ALTER TABLE eval_runs ADD COLUMN git_dirty INTEGER;
ALTER TABLE eval_runs ADD COLUMN persona_hash TEXT;
ALTER TABLE eval_runs ADD COLUMN repeats INTEGER NOT NULL DEFAULT 1;
ALTER TABLE eval_runs ADD COLUMN tier TEXT;
ALTER TABLE eval_runs ADD COLUMN case_filter TEXT;
ALTER TABLE eval_runs ADD COLUMN total_tokens INTEGER;
ALTER TABLE eval_runs ADD COLUMN total_cost_usd REAL;
ALTER TABLE eval_runs ADD COLUMN notes TEXT;

ALTER TABLE eval_case_results ADD COLUMN repeat_index INTEGER NOT NULL DEFAULT 0;
ALTER TABLE eval_case_results ADD COLUMN failure_class TEXT;
ALTER TABLE eval_case_results ADD COLUMN prompt_tokens INTEGER;
ALTER TABLE eval_case_results ADD COLUMN completion_tokens INTEGER;
ALTER TABLE eval_case_results ADD COLUMN cost_usd REAL;
ALTER TABLE eval_case_results ADD COLUMN llm_calls INTEGER;
ALTER TABLE eval_case_results ADD COLUMN judge_items_json TEXT;
ALTER TABLE eval_case_results ADD COLUMN tool_calls_json TEXT;
ALTER TABLE eval_case_results ADD COLUMN deliveries_json TEXT;
ALTER TABLE eval_case_results ADD COLUMN flags_json TEXT;
