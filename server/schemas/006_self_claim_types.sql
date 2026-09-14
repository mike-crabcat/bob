-- 006: self-bob typed vocabulary (2026-09-14 self-memory review).
-- self-bob had 6 claim types for everything, so limit/capability became a
-- landfill of incident stories, tool how-tos and build logs (70 active
-- limits, ~66% noise). These four types give each class a real home;
-- `self_state` is single-active per facet (enforced in write_claim).
-- Registry twins live in server/services/memory/claim_types.py (_RAW_TYPES).
INSERT OR IGNORE INTO "memory_claim_types" ("key", "applicable_types", "description", "example") VALUES
('self_state', '["self"]', 'Bob''s CURRENT configuration/runtime facts, one facet per claim, value format ''facet: value'' (e.g. ''primary model: glm-5.3-flash''). Writing a new value for the same facet supersedes the previous one automatically — never append history or diary ops events here', 'self-bob → "primary model: glm-5.3-flash (OpenRouter)"'),
('practice', '["self"]', 'A standing operational rule Bob has learned from experience — a ''should'', not a ''can''t''. One imperative rule per claim, stated as the rule itself; no incident narrative, no dates', 'self-bob → "Query the actual source system before answering schedule/state questions — chat context goes stale"'),
('feedback', '["self"]', 'An attributed human assessment of Bob''s performance: who said it, roughly when, what they said. Real dates only — never a future date. Recurring themes update one claim rather than stacking new ones', 'self-bob → "Brad (2026-09-11, phone feedback): latency pretty good; voice reads British, would prefer Californian"'),
('incident', '["self"]', 'A dated one-off episode or postmortem — what went wrong and when. The ONLY claim type for episode narratives; reconciliation distils incidents into practice claims and retires old ones', 'self-bob → "2026-08-30: sent Sean''s tee-order confirmation (with address) to the AI Doom group instead of his DM"');
