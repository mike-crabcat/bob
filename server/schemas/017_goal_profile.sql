-- Goal profiles (commitments plan Phase 1, 2026-10-05): behaviour routes off
-- a CLOSED profile, never off the free-text kind. The 2026-09-25 incident:
-- kind 'event' vs 'event_plan' silently decided whether a goal got its
-- room, loop and playbook. kind stays as a free label (display, playbook
-- text, budget tuning — advisory only).
--
-- profile: outcome — multi-step objective; gets a room + loop
--          promise — a leaf owed by someone/something else (a contact's
--                    reply, a call, an email thread, a subagent); no room

ALTER TABLE goals ADD COLUMN profile TEXT NOT NULL DEFAULT 'outcome';

UPDATE goals SET profile = 'promise'
WHERE kind IN ('subagent', 'call', 'email_thread', 'outreach');
