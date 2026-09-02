-- Add full messages JSON to harness logs for debugging
ALTER TABLE harness_logs ADD COLUMN messages_json TEXT DEFAULT '[]';
