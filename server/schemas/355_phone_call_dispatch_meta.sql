-- Durable dispatch metadata for realtime voice calls.
--
-- Previously the instructions / voice / subagent_id for an outbound realtime
-- call lived only in an in-memory dict (call_agendas), so a server restart
-- between dial and answer killed the call, and kill_subagent couldn't hang up
-- because the call_sid was unreachable. These columns make phone_calls the
-- source of truth; the dict is just a hot-path cache.
--
-- `outcome` stores the structured report_success / report_failure tool result
-- (JSON) instead of mashing tool output into the transcript prose. Mirrored on
-- voice_sessions for browser voice-link calls.

ALTER TABLE phone_calls ADD COLUMN realtime_meta TEXT NOT NULL DEFAULT '{}';
ALTER TABLE phone_calls ADD COLUMN subagent_id TEXT;
ALTER TABLE phone_calls ADD COLUMN result_dispatched_at TEXT;
ALTER TABLE phone_calls ADD COLUMN outcome TEXT;
ALTER TABLE voice_sessions ADD COLUMN outcome TEXT;
