-- Goal-oriented voice sessions for the unified reach_out_with_voice_call tool.
-- `goal` carries the task instruction (e.g. "find out when Alice is free next week");
-- empty for the persona-only `initiate_voice_call` flow.
-- `report_back_session_key` is an optional second dispatch target — when set, the
-- call summary lands in BOTH the origin session and this one (so the user who
-- asked for the reach-out gets the answer).
ALTER TABLE voice_sessions ADD COLUMN goal TEXT NOT NULL DEFAULT '';
ALTER TABLE voice_sessions ADD COLUMN report_back_session_key TEXT;
