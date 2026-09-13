-- 005: group norm/tradition + person work_schedule claim types.
-- Learned-expectation vocabulary for the memory graph (2026-09-13 AI doom
-- lunch incident): behavior expectations live as typed claims on the group
-- entity so they survive task completion and can be pushed into group turns.
-- Registry twins live in server/services/memory/claim_types.py (_RAW_TYPES).
INSERT OR IGNORE INTO "memory_claim_types" ("key", "applicable_types", "description", "example") VALUES
('norm', '["group"]', 'Durable behavioral expectation for Bob in this group, derived from a correction or explicit decision (e.g. ''lunches are members-only'', ''Bob proposes, group decides''). Outlives any single task or event; punchy one-fact values. NOT observations of group culture (that''s vibe) and NOT one-off arrangements', 'group-ai-doom → "AI Doom lunches are adults-only, chat members only — never apply Mike''s family context"'),
('tradition', '["group"]', 'A recurring practice or series the group has established — how it usually runs, cadence, last instance. NOT a single event (use event entities)', 'group-ai-doom → "recurring group lunch series; #1 The Stables 2026-09-01 (Mike, David, Rupert)"'),
('work_schedule', '["person"]', 'Usual weekly work pattern — WFH weekdays, office/city days, recurring standing commitments that constrain daytime scheduling (e.g. ''WFH Fridays only'', ''recurring Thursday morning meeting''). Only from their own stated answer, never inferred', 'person-mike-cleaver → "WFH Fridays only; in the city Mon–Thu"');
