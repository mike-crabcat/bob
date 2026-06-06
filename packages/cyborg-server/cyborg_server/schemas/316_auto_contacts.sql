-- Allow NULL phone_number for auto-created contacts (name-only from entity extraction)

PRAGMA foreign_keys = OFF;

CREATE TABLE contacts_new (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    phone_number TEXT UNIQUE,
    email TEXT UNIQUE,
    metadata TEXT,
    is_default INTEGER NOT NULL DEFAULT 0,
    is_trusted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

INSERT INTO contacts_new
    (id, name, phone_number, email, metadata, is_default, is_trusted, created_at, updated_at, deleted_at)
SELECT
    id, name, phone_number, email, metadata, is_default, is_trusted, created_at, updated_at, deleted_at
FROM contacts;

DROP TABLE contacts;
ALTER TABLE contacts_new RENAME TO contacts;

CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_phone_number ON contacts(phone_number) WHERE phone_number IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email) WHERE email IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_contacts_name ON contacts(name);
CREATE INDEX IF NOT EXISTS idx_contacts_deleted_at ON contacts(deleted_at);

PRAGMA foreign_keys = ON;
