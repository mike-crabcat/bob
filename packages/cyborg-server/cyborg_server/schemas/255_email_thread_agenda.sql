-- Store the agenda prompt used to seed the OpenClaw session for this thread.
ALTER TABLE email_threads ADD COLUMN agenda TEXT;
