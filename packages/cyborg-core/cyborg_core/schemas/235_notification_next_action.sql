-- Add 'next_action' to the notification_type CHECK constraint.
-- SQLite requires a table rebuild to modify CHECK constraints.

DROP TABLE IF EXISTS notifications_v8;
CREATE TABLE notifications_v8 (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('task', 'project', 'event')),
    entity_id TEXT NOT NULL,
    notification_type TEXT NOT NULL CHECK (
        notification_type IN ('needs_input', 'event_reminder', 'task_assignment', 'task_result', 'project_result', 'task_retry', 'task_input_response', 'task_tap', 'submission_review', 'next_action')
    ),
    status TEXT NOT NULL CHECK (status IN ('pending', 'acknowledged', 'resolved')),
    delivery_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        delivery_status IN ('pending', 'sending', 'delivered', 'failed')
    ),
    delivery_attempt_count INTEGER NOT NULL DEFAULT 0,
    last_delivery_at TEXT,
    last_delivery_error TEXT,
    next_delivery_at TEXT,
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

INSERT INTO notifications_v8 (
    id, entity_type, entity_id, notification_type, status,
    delivery_status, delivery_attempt_count, last_delivery_at, last_delivery_error, next_delivery_at,
    title, message, metadata, sequence_number,
    created_at, updated_at, acknowledged_at, acknowledged_by, resolved_at, source_updated_at
)
SELECT
    id, entity_type, entity_id, notification_type, status,
    delivery_status, delivery_attempt_count, last_delivery_at, last_delivery_error, next_delivery_at,
    title, message, metadata, sequence_number,
    created_at, updated_at, acknowledged_at, acknowledged_by, resolved_at, source_updated_at
FROM notifications;

DROP TABLE notifications;
ALTER TABLE notifications_v8 RENAME TO notifications;

CREATE INDEX IF NOT EXISTS idx_notifications_status_created_at
    ON notifications(status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_notifications_entity
    ON notifications(entity_type, entity_id, notification_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_notifications_delivery_due
    ON notifications(status, delivery_status, next_delivery_at);
