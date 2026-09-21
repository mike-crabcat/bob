-- Goal execution loop (docs/goal-execution-plan.md). System-owned runtime
-- state for the continuation contract: budget counters, spin/stall streaks,
-- the pending end-of-turn declaration cell, and the frame the last wake
-- carried. Additive only; NULL on pre-loop goals (loop inert until
-- ensure_loop seeds it — the BOB_GOAL_LOOP switch defaults off).
ALTER TABLE goals ADD COLUMN loop_state_json TEXT;
