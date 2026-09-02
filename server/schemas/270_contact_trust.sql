-- Add trusted contact flag for three-tier trust model
-- Unknown sender (not in contacts) -> UNTRUSTED_EXTERNAL_AGENDA
-- Known but untrusted (is_trusted = 0) -> KNOWN_UNTRUSTED_AGENDA
-- Trusted (is_trusted = 1) -> DEFAULT_AGENDA

ALTER TABLE contacts ADD COLUMN is_trusted INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_contacts_trusted
ON contacts(is_trusted) WHERE deleted_at IS NULL;
