-- FTS5 virtual table for fast keyword pre-filtering of memory entities.
CREATE VIRTUAL TABLE IF NOT EXISTS memory_entities_fts USING fts5(
    entity_id,
    display_name,
    body,
    content='memory_entities',
    content_rowid='rowid'
);

-- Keep FTS in sync with inserts.
CREATE TRIGGER IF NOT EXISTS memory_entities_ai AFTER INSERT ON memory_entities BEGIN
    INSERT INTO memory_entities_fts(rowid, entity_id, display_name, body)
    VALUES (new.rowid, new.entity_id, new.display_name, new.body);
END;

-- Keep FTS in sync with deletes.
CREATE TRIGGER IF NOT EXISTS memory_entities_ad AFTER DELETE ON memory_entities BEGIN
    INSERT INTO memory_entities_fts(memory_entities_fts, rowid, entity_id, display_name, body)
    VALUES ('delete', old.rowid, old.entity_id, old.display_name, old.body);
END;

-- Keep FTS in sync with updates.
CREATE TRIGGER IF NOT EXISTS memory_entities_au AFTER UPDATE ON memory_entities BEGIN
    INSERT INTO memory_entities_fts(memory_entities_fts, rowid, entity_id, display_name, body)
    VALUES ('delete', old.rowid, old.entity_id, old.display_name, old.body);
    INSERT INTO memory_entities_fts(rowid, entity_id, display_name, body)
    VALUES (new.rowid, new.entity_id, new.display_name, new.body);
END;

-- Populate from existing data.
INSERT INTO memory_entities_fts(rowid, entity_id, display_name, body)
    SELECT rowid, entity_id, display_name, body FROM memory_entities;
