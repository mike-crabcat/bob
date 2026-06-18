-- Persist tool-call traces on assistant rows so subsequent dispatches can
-- replay prior tool_use/tool_result context. See build_chat_messages in
-- services/prompt_assembler.py for the read side.

ALTER TABLE session_messages ADD COLUMN tool_summary TEXT;
ALTER TABLE session_messages ADD COLUMN tool_blocks_json TEXT;
