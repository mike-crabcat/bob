-- FTS v2: indexes rendered body from templates instead of stored body column.
-- Standalone FTS5 table (not content=synced) — application layer manages all content.

DROP TABLE IF EXISTS memory_entities_fts;
DROP TRIGGER IF EXISTS memory_entities_ai;
DROP TRIGGER IF EXISTS memory_entities_ad;
DROP TRIGGER IF EXISTS memory_entities_au;

-- Standalone FTS5: no content= reference, no triggers needed.
-- Application must use INSERT/DELETE directly to keep FTS in sync.
CREATE VIRTUAL TABLE IF NOT EXISTS memory_entities_fts USING fts5(
    entity_id,
    display_name,
    rendered_body
);

-- Application layer is responsible for keeping FTS in sync.
-- After any claim change, the service calls render_entity() and upserts the FTS row.
--
-- In Python:
--   rendered = render_entity(entity_type, display_name, claims)
--   db.execute("DELETE FROM memory_entities_fts WHERE entity_id = ?", (entity_id,))
--   db.execute("INSERT INTO memory_entities_fts(entity_id, display_name, rendered_body) VALUES (?, ?, ?)",
--              (entity_id, display_name, rendered))
