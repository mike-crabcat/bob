-- Persist the dispatch's own tool-call trace on llm_call_log so the dashboard
-- can show this dispatch's tool calls without parsing messages_json (which now
-- also contains replayed tool items from prior dispatches — see prompt_assembler
-- build_chat_messages). Mirrors tool_blocks_json on session_messages.

ALTER TABLE llm_call_log ADD COLUMN tool_blocks_json TEXT;
