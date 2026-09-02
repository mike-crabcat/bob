-- Store the agenda prompt used to seed the LLM session for this thread.
ALTER TABLE email_threads ADD COLUMN agenda TEXT;
