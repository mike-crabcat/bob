-- OpenRouter serving attribution (2026-09-26). glm-5.3-flash is routed
-- across a 33-endpoint pool including fp4/nvfp4 and undeclared-quant hosts;
-- quality incidents (e.g. the 2026-09-17 spurious-tools confabulation)
-- could not be attributed to a serving provider afterwards. The Responses
-- API returns the generation id in-band but NOT the provider, so:
--   generation_id — captured at request time from the response
--   served_by / served_quant — resolved lazily by the heartbeat
--     OpenRouterAttributionTask via GET /api/v1/generation?id=…
ALTER TABLE llm_call_log ADD COLUMN generation_id TEXT;
ALTER TABLE llm_call_log ADD COLUMN served_by TEXT;
ALTER TABLE llm_call_log ADD COLUMN served_quant TEXT;
