-- Create whatsappgroups and whatsappgroup_members tables,
-- and remove whatsapp_groups column from contacts.

-- 1. Create whatsappgroups table

CREATE TABLE IF NOT EXISTS whatsappgroups (
    id TEXT PRIMARY KEY,
    whatsapp_jid TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    description TEXT,
    member_count INTEGER NOT NULL DEFAULT 0,
    metadata TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_whatsappgroups_whatsapp_jid ON whatsappgroups(whatsapp_jid);
CREATE INDEX IF NOT EXISTS idx_whatsappgroups_name ON whatsappgroups(name);
CREATE INDEX IF NOT EXISTS idx_whatsappgroups_deleted_at ON whatsappgroups(deleted_at);

-- 2. Create whatsappgroup_members table

CREATE TABLE IF NOT EXISTS whatsappgroup_members (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES whatsappgroups(id),
    contact_id TEXT NOT NULL REFERENCES contacts(id),
    is_admin INTEGER NOT NULL DEFAULT 0,
    is_super_admin INTEGER NOT NULL DEFAULT 0,
    display_name TEXT NOT NULL DEFAULT '',
    joined_at TEXT NOT NULL,
    left_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(group_id, contact_id)
);

CREATE INDEX IF NOT EXISTS idx_whatsappgroup_members_group_id ON whatsappgroup_members(group_id);
CREATE INDEX IF NOT EXISTS idx_whatsappgroup_members_contact_id ON whatsappgroup_members(contact_id);

-- 3. Remove whatsapp_groups column from contacts (SQLite requires table recreation)

CREATE TABLE IF NOT EXISTS contacts_new (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    phone_number TEXT NOT NULL UNIQUE,
    email TEXT UNIQUE,
    metadata TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

INSERT INTO contacts_new
    (id, name, phone_number, email, metadata, created_at, updated_at, deleted_at)
SELECT
    id, name, phone_number, email, metadata, created_at, updated_at, deleted_at
FROM contacts;

DROP TABLE contacts;
ALTER TABLE contacts_new RENAME TO contacts;

-- Recreate indexes from 80_contacts.sql (minus the whatsapp_groups column)
CREATE INDEX IF NOT EXISTS idx_contacts_phone_number ON contacts(phone_number);
CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email);
CREATE INDEX IF NOT EXISTS idx_contacts_name ON contacts(name);
CREATE INDEX IF NOT EXISTS idx_contacts_deleted_at ON contacts(deleted_at);
