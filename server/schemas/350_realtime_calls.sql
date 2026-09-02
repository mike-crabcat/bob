-- OpenAI Realtime voice calls.
-- `engine` selects the call's voice pipeline: 'default' (STT→LLM→TTS) or
-- 'openai_realtime' (end-to-end Realtime bridge via services/realtime_bridge.py).
-- `transcript` stores the assistant-side transcript captured by the Realtime
-- bridge for outbound task calls (the default pipeline logs per-exchange in
-- phone_call_exchanges instead).
ALTER TABLE phone_calls ADD COLUMN engine TEXT NOT NULL DEFAULT 'default';
ALTER TABLE phone_calls ADD COLUMN transcript TEXT NOT NULL DEFAULT '';
