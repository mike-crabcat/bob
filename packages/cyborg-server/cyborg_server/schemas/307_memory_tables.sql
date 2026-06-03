-- Memory system: bulletins, claims, entities, relations, aliases.

-- Bulletins: immutable source records (strongly typed)
CREATE TABLE IF NOT EXISTS memory_bulletins (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    channel_id TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    transcript_range_id TEXT NOT NULL DEFAULT '',
    visibility TEXT NOT NULL DEFAULT 'channel'
        CHECK(visibility IN ('private','contact','group','channel','public')),
    scope TEXT NOT NULL DEFAULT '[]',
    memory_types TEXT NOT NULL DEFAULT '[]',
    confidence TEXT NOT NULL DEFAULT 'medium'
        CHECK(confidence IN ('high','medium','low')),
    requires_review INTEGER NOT NULL DEFAULT 0,
    review_reasons TEXT NOT NULL DEFAULT '[]',
    content TEXT NOT NULL DEFAULT '',
    digested INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_memory_bulletins_created
    ON memory_bulletins(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_bulletins_channel
    ON memory_bulletins(channel_id);
CREATE INDEX IF NOT EXISTS idx_memory_bulletins_digested
    ON memory_bulletins(digested, created_at DESC);

-- Bulletin entity refs (normalized from entities dict)
CREATE TABLE IF NOT EXISTS memory_bulletin_entities (
    bulletin_id TEXT NOT NULL,
    category TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    display_name TEXT,
    resolution_status TEXT NOT NULL DEFAULT 'known'
        CHECK(resolution_status IN ('known','unresolved','ambiguous','proposed','resolved')),
    role TEXT,
    PRIMARY KEY (bulletin_id, category, entity_id),
    FOREIGN KEY (bulletin_id) REFERENCES memory_bulletins(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_memory_bulletin_entities_entity
    ON memory_bulletin_entities(entity_id);

-- Claims: atomic typed memories
CREATE TABLE IF NOT EXISTS memory_claims (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL
        CHECK(type IN ('fact','preference','constraint','decision','task',
                       'availability','booking','artifact','relationship','private_note')),
    subject_id TEXT NOT NULL,
    predicate TEXT NOT NULL DEFAULT '',
    object_id TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','superseded','retracted','expired',
                         'disputed','archived')),
    visibility TEXT NOT NULL DEFAULT 'channel'
        CHECK(visibility IN ('private','contact','group','channel','public')),
    scope TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    superseded_by TEXT NOT NULL DEFAULT '[]',
    source_bulletins TEXT NOT NULL DEFAULT '[]',
    body TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_memory_claims_subject
    ON memory_claims(subject_id);
CREATE INDEX IF NOT EXISTS idx_memory_claims_object
    ON memory_claims(object_id);
CREATE INDEX IF NOT EXISTS idx_memory_claims_status
    ON memory_claims(status);
CREATE INDEX IF NOT EXISTS idx_memory_claims_type
    ON memory_claims(type);

-- Entity documents: derived current-state views
CREATE TABLE IF NOT EXISTS memory_entities (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL
        CHECK(entity_type IN ('channel','contact','group','location','trip',
                              'event','task','artifact','decision')),
    display_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','archived')),
    extra_frontmatter TEXT NOT NULL DEFAULT '{}',
    body TEXT NOT NULL DEFAULT '',
    source_bulletins TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_memory_entities_type
    ON memory_entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_memory_entities_display_name
    ON memory_entities(display_name);

-- Entity relations (normalized related_entities)
CREATE TABLE IF NOT EXISTS memory_entity_relations (
    source_entity_id TEXT NOT NULL,
    category TEXT NOT NULL,
    target_entity_id TEXT NOT NULL,
    PRIMARY KEY (source_entity_id, category, target_entity_id),
    FOREIGN KEY (source_entity_id) REFERENCES memory_entities(entity_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_memory_entity_relations_target
    ON memory_entity_relations(target_entity_id);

-- Aliases: display_name -> entity_id lookup
CREATE TABLE IF NOT EXISTS memory_aliases (
    alias TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL,
    FOREIGN KEY (entity_id) REFERENCES memory_entities(entity_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_memory_aliases_entity
    ON memory_aliases(entity_id);
