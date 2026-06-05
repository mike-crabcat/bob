ALTER TABLE memory_bulletins ADD COLUMN session_range_start TEXT NOT NULL DEFAULT '';
ALTER TABLE memory_bulletins ADD COLUMN session_range_end TEXT NOT NULL DEFAULT '';
CREATE INDEX idx_memory_bulletins_session_range
  ON memory_bulletins(source_type, source_id, session_range_end DESC);
