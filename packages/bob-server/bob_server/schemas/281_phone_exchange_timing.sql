-- Add precise start timestamp to phone call exchanges for timeline display.

ALTER TABLE phone_call_exchanges ADD COLUMN started_at TEXT;
