-- Entity embedding vectors for semantic search via sqlite-vec.
CREATE VIRTUAL TABLE IF NOT EXISTS memory_entity_embeddings
USING vec0(
    entity_id TEXT PRIMARY KEY,
    embedding float[1536]
);
