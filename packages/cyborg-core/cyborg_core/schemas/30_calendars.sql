CREATE TABLE IF NOT EXISTS calendars (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    color TEXT,
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_calendars_deleted_at ON calendars(deleted_at);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    calendar_id TEXT NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT,
    agenda TEXT,
    venue TEXT,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    timezone TEXT NOT NULL,
    is_all_day INTEGER NOT NULL DEFAULT 0,
    recurrence_rule TEXT,
    status TEXT NOT NULL CHECK (status IN ('tentative', 'confirmed', 'cancelled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_calendar_id ON events(calendar_id);
CREATE INDEX IF NOT EXISTS idx_events_start_time ON events(start_time);
CREATE INDEX IF NOT EXISTS idx_events_deleted_at ON events(deleted_at);

CREATE TABLE IF NOT EXISTS event_recipients (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    recipient_type TEXT NOT NULL CHECK (recipient_type IN ('email', 'phone', 'channel')),
    recipient_address TEXT NOT NULL,
    name TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'confirmed', 'declined', 'tentative')),
    responded_at TEXT,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_event_recipients_event_id ON event_recipients(event_id);
