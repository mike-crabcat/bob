-- Time-series GPS pings fetched from Home Assistant on a 15-min schedule.
-- Deliberately separate from memory_claims (which are slowly-changing facts).

CREATE TABLE IF NOT EXISTS location_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at TEXT NOT NULL,                       -- when Bob queried HA (ISO UTC)
    device_tracker_entity_id TEXT NOT NULL,
    latitude REAL,
    longitude REAL,
    gps_accuracy REAL,                              -- meters, nullable
    zone_state TEXT,                                -- 'home' / 'not_home' / zone name
    battery_level REAL,                             -- nullable
    ha_last_updated TEXT,                           -- when HA last got data from phone
    raw_attributes TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_location_history_fetched
    ON location_history(fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_location_history_device_time
    ON location_history(device_tracker_entity_id, fetched_at DESC);
