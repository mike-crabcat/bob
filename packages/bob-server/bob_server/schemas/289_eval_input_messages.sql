-- Store input messages for re-judging without re-calling production LLM.
ALTER TABLE eval_case_results ADD COLUMN input_messages_json TEXT;
