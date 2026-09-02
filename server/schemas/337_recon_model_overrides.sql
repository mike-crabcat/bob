-- Per-entity reconciliation model overrides.
-- When a row exists for an entity_id, its model is used for reconciliation
-- regardless of the default or per-entity-type config.
CREATE TABLE IF NOT EXISTS recon_model_overrides (
    entity_id TEXT PRIMARY KEY,
    model     TEXT NOT NULL,
    reason    TEXT NOT NULL DEFAULT '',
    set_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
