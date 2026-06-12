ALTER TABLE phone_call_exchanges ADD COLUMN gateway_prepare_ms INTEGER;
ALTER TABLE phone_call_exchanges ADD COLUMN gateway_stream_ms INTEGER;
ALTER TABLE phone_call_exchanges ADD COLUMN tts_wait_lock_ms INTEGER;
ALTER TABLE phone_call_exchanges ADD COLUMN tts_generate_ms INTEGER;
