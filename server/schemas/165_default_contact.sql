-- Add default contact flag for notification routing fallback
-- When no route is resolved, notifications will be sent to the default contact

ALTER TABLE contacts ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0;

-- Ensure only one contact can be default
CREATE TRIGGER IF NOT EXISTS contacts_single_default
BEFORE UPDATE OF is_default ON contacts
WHEN NEW.is_default = 1
BEGIN
    UPDATE contacts SET is_default = 0 WHERE id != NEW.id AND deleted_at IS NULL;
END;

CREATE TRIGGER IF NOT EXISTS contacts_single_default_insert
BEFORE INSERT ON contacts
WHEN NEW.is_default = 1
BEGIN
    UPDATE contacts SET is_default = 0 WHERE id != NEW.id AND deleted_at IS NULL;
END;

CREATE INDEX IF NOT EXISTS idx_contacts_default ON contacts(is_default) WHERE is_default = 1 AND deleted_at IS NULL;
