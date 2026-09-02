-- Dream v2 fix: the embeddings table was created without an explicit distance
-- metric, so sqlite-vec defaulted to L2 (euclidean) while the dedup threshold
-- (0.25) was calibrated for cosine. Same-topic paraphrases measured ~0.5 L2 but
-- ~0.12 cosine, so dedup silently matched nothing. Recreate with cosine and
-- re-embed items via `bob dream reindex`.
DROP TABLE IF EXISTS dream_item_embeddings;
CREATE VIRTUAL TABLE dream_item_embeddings
USING vec0(
    item_id TEXT PRIMARY KEY,
    embedding float[1536] distance_metric=cosine
);
