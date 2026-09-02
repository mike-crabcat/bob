-- Track direct delivery attempts for notifications.

ALTER TABLE notifications ADD COLUMN delivery_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE notifications ADD COLUMN delivery_attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE notifications ADD COLUMN last_delivery_at TEXT;
ALTER TABLE notifications ADD COLUMN last_delivery_error TEXT;
ALTER TABLE notifications ADD COLUMN next_delivery_at TEXT;

UPDATE notifications
SET delivery_status = COALESCE(delivery_status, 'pending'),
    delivery_attempt_count = COALESCE(delivery_attempt_count, 0),
    next_delivery_at = COALESCE(next_delivery_at, created_at)
WHERE 1 = 1;

CREATE INDEX IF NOT EXISTS idx_notifications_delivery_due
ON notifications(status, delivery_status, next_delivery_at);
