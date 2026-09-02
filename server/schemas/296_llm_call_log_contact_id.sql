-- Add contact_id to llm_call_log so the dashboard can show which
-- participant triggered each LLM call.
ALTER TABLE llm_call_log ADD COLUMN contact_id TEXT;

CREATE INDEX IF NOT EXISTS idx_llm_call_log_contact
ON llm_call_log(contact_id) WHERE contact_id IS NOT NULL;
