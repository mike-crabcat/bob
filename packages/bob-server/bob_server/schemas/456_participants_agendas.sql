-- Increment 6a: session_participants -> participants, session_agendas -> agendas,
-- keyed by canonical conversation_id (resolved via bindings; falls back to the
-- session_key itself since conversations.id == legacy session_key 1:1).
-- Merge collision rules: newest row (last_active_at / updated_at) wins for
-- names/contact/agenda text; trust is MAX across merged rows.

-- Conversations may be missing for very old participant rows that predate the
-- 430 backfill (including dangling bindings); create bare rows so the FK holds.
INSERT OR IGNORE INTO conversations (id, kind, created_at, updated_at)
SELECT DISTINCT COALESCE(b.conversation_id, sp.session_key),
       CASE WHEN sp.session_key LIKE '%:group:%' THEN 'group'
            WHEN sp.session_key LIKE '%:dm:%' THEN 'dm'
            WHEN sp.session_key LIKE '%:thread:%' THEN 'thread'
            ELSE 'internal' END,
       datetime('now'), datetime('now')
FROM session_participants sp
LEFT JOIN bindings b ON b.session_key = sp.session_key;

INSERT OR IGNORE INTO conversations (id, kind, created_at, updated_at)
SELECT DISTINCT COALESCE(b.conversation_id, sa.session_key),
       CASE WHEN sa.session_key LIKE '%:group:%' THEN 'group'
            WHEN sa.session_key LIKE '%:dm:%' THEN 'dm'
            WHEN sa.session_key LIKE '%:thread:%' THEN 'thread'
            ELSE 'internal' END,
       datetime('now'), datetime('now')
FROM session_agendas sa
LEFT JOIN bindings b ON b.session_key = sa.session_key;

-- Null out references to hard-deleted contacts (data-quality exceptions).
UPDATE session_participants SET contact_id = NULL
WHERE contact_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM contacts c WHERE c.id = session_participants.contact_id);

CREATE TABLE participants (
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    identifier TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    contact_id TEXT REFERENCES contacts(id),
    is_trusted INTEGER NOT NULL DEFAULT 0,
    last_active_at TEXT NOT NULL,
    PRIMARY KEY (conversation_id, identifier)
);

INSERT INTO participants (conversation_id, identifier, display_name, contact_id, is_trusted, last_active_at)
SELECT cid, identifier, display_name, contact_id, is_trusted, last_active_at
FROM (
    SELECT COALESCE(b.conversation_id, sp.session_key) AS cid,
           sp.identifier, sp.display_name, sp.contact_id, sp.is_trusted, sp.last_active_at,
           ROW_NUMBER() OVER (
               PARTITION BY COALESCE(b.conversation_id, sp.session_key), sp.identifier
               ORDER BY sp.last_active_at DESC) AS rn
    FROM session_participants sp
    LEFT JOIN bindings b ON b.session_key = sp.session_key
)
WHERE rn = 1;

-- Trust is MAX across rows a merge collapsed together.
UPDATE participants SET is_trusted = 1
WHERE is_trusted = 0 AND EXISTS (
    SELECT 1 FROM session_participants sp
    LEFT JOIN bindings b ON b.session_key = sp.session_key
    WHERE COALESCE(b.conversation_id, sp.session_key) = participants.conversation_id
      AND sp.identifier = participants.identifier
      AND sp.is_trusted = 1
);

CREATE INDEX idx_participants_contact ON participants(contact_id) WHERE contact_id IS NOT NULL;

DROP TABLE session_participants;

CREATE TABLE agendas (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id),
    agenda TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO agendas (conversation_id, agenda, updated_at)
SELECT cid, agenda, updated_at
FROM (
    SELECT COALESCE(b.conversation_id, sa.session_key) AS cid,
           sa.agenda, sa.updated_at,
           ROW_NUMBER() OVER (
               PARTITION BY COALESCE(b.conversation_id, sa.session_key)
               ORDER BY sa.updated_at DESC) AS rn
    FROM session_agendas sa
    LEFT JOIN bindings b ON b.session_key = sa.session_key
)
WHERE rn = 1;

DROP TABLE session_agendas;
