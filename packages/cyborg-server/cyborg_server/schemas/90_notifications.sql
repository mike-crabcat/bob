ALTER TABLE tasks ADD COLUMN notification_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN last_notification_at TEXT;
ALTER TABLE tasks ADD COLUMN needs_input_since TEXT;

ALTER TABLE projects ADD COLUMN notification_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE projects ADD COLUMN last_notification_at TEXT;
ALTER TABLE projects ADD COLUMN needs_input_since TEXT;

CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('task', 'project', 'event')),
    entity_id TEXT NOT NULL,
    notification_type TEXT NOT NULL CHECK (notification_type IN ('needs_input', 'event_reminder')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'acknowledged', 'resolved')),
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    sequence_number INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    acknowledged_at TEXT,
    acknowledged_by TEXT,
    resolved_at TEXT,
    source_updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_notifications_status_created_at
    ON notifications(status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_notifications_entity
    ON notifications(entity_type, entity_id, notification_type, created_at DESC);
