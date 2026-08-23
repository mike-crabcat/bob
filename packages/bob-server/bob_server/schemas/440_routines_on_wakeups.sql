-- Bob3: routines ride the unified wakeups mechanism (deferred-work item).
-- The routines table remains the definition store (name, cron, prompt,
-- validity); scheduling/firing moves to the wakeups pump. RoutineSchedulerTask
-- is deleted.

ALTER TABLE wakeups ADD COLUMN kind TEXT NOT NULL DEFAULT 'wake';       -- wake|routine
ALTER TABLE wakeups ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}';

-- Backfill: one scheduled wakeup per enabled routine, carrying the cron spec
-- as recurrence. conversation_id = session_key (Phase VI backfill convention).
INSERT INTO wakeups (id, conversation_id, goal_id, not_before, recurrence, tz,
                     status, created_at, kind, payload_json)
SELECT lower(hex(randomblob(16))),
       r.session_key,
       NULL,
       r.next_run_at,
       'cron:' || r.schedule,
       r.timezone,
       'scheduled',
       datetime('now'),
       'routine',
       json_object('routine_id', r.id)
FROM routines r
WHERE r.enabled = 1
  AND NOT EXISTS (
      SELECT 1 FROM wakeups w
      WHERE w.kind = 'routine' AND w.status = 'scheduled'
        AND json_extract(w.payload_json, '$.routine_id') = r.id
  );
