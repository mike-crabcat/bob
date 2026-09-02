-- Webhook configuration and delivery tracking

-- Webhook configurations
CREATE TABLE IF NOT EXISTS webhook_configs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL,
    secret TEXT NOT NULL,  -- For HMAC signature
    events TEXT NOT NULL,  -- JSON array of event types
    retry_count INTEGER NOT NULL DEFAULT 3,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_webhook_configs_active ON webhook_configs(is_active);
CREATE INDEX IF NOT EXISTS idx_webhook_configs_deleted_at ON webhook_configs(deleted_at);

-- Webhook delivery log
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id TEXT PRIMARY KEY,
    webhook_id TEXT NOT NULL REFERENCES webhook_configs(id),
    event TEXT NOT NULL,
    payload TEXT NOT NULL,  -- JSON payload sent
    status TEXT NOT NULL CHECK (status IN ('pending', 'delivered', 'failed')),
    response_code INTEGER,
    response_body TEXT,
    error_message TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    created_at TEXT NOT NULL,
    delivered_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_webhook_id ON webhook_deliveries(webhook_id);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_status ON webhook_deliveries(status);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_event ON webhook_deliveries(event);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_created_at ON webhook_deliveries(created_at);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_next_retry ON webhook_deliveries(next_retry_at) WHERE status = 'pending';
