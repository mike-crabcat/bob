-- Tasks join the goals tree (commitments plan Phase 2, 2026-10-05).
-- A task is a promise-profile goal (kind 'promise'): owed by a completer,
-- settled CAS-once, the waiter woken via the task_settle effect. Ids are
-- preserved ('task-<hex8>'), so every reference — wakeup payloads, wake
-- metadata, chat mentions — keeps resolving.
--
-- Isolation by design: promises keep the task status vocabulary
-- (pending | completed | failed | cancelled). Every goal sweeper (reviser,
-- loop, deadline, close-achieved, goals_block) scans status = 'active', so
-- none of them ever sees a promise; the task machinery owns promises.
--
-- The tasks table is FROZEN as an archive after this copy.

ALTER TABLE goals ADD COLUMN completer TEXT;       -- task expected_completer
ALTER TABLE goals ADD COLUMN payload_json TEXT;    -- context for the completer
ALTER TABLE goals ADD COLUMN error TEXT;
ALTER TABLE goals ADD COLUMN completed_by TEXT;    -- settle provenance
ALTER TABLE goals ADD COLUMN completed_at TEXT;
ALTER TABLE goals ADD COLUMN delivered_at TEXT;    -- waiter woken, exactly-once
ALTER TABLE goals ADD COLUMN source_goal_id TEXT;  -- registering room's goal
ALTER TABLE goals ADD COLUMN refs_json TEXT;

INSERT INTO goals (id, conversation_id, origin_conversation_id, kind, profile,
                   objective, status, result, deadline, payload_json, completer,
                   error, completed_by, completed_at, delivered_at,
                   source_goal_id, refs_json, version, created_at, updated_at)
SELECT id, waiter_session, waiter_session, 'promise', 'promise',
       title, status, result, due, payload_json, expected_completer,
       error, completed_by, completed_at, delivered_at,
       source_goal_id, refs_json, 1, created_at, updated_at
FROM tasks
WHERE id NOT IN (SELECT id FROM goals);

CREATE INDEX IF NOT EXISTS idx_goals_promise_waiter
    ON goals (origin_conversation_id, status) WHERE kind = 'promise';
CREATE INDEX IF NOT EXISTS idx_goals_promise_completer
    ON goals (completer, status) WHERE kind = 'promise';
CREATE INDEX IF NOT EXISTS idx_goals_promise_source
    ON goals (source_goal_id, status) WHERE kind = 'promise';
CREATE INDEX IF NOT EXISTS idx_goals_promise_due
    ON goals (status, deadline) WHERE kind = 'promise';
