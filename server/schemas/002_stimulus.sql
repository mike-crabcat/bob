-- Stimulus spine (docs/stimulus-spine-plan.md): external feeds raise events,
-- routes decide which conversation gets woken. Sources POST envelopes to
-- /api/v1/stimulus/events (dedicated bearer token); the heartbeat router
-- drains pending events into steers per stimulus_routes.

CREATE TABLE IF NOT EXISTS stimulus_events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              TEXT NOT NULL,               -- ISO UTC, source-stamped
  source          TEXT NOT NULL,               -- 'cryptobro', 'homeassistant', ...
  type            TEXT NOT NULL,               -- 'signal.momentum', 'zone.enter', ...
  level           TEXT NOT NULL DEFAULT 'info' CHECK (level IN ('info', 'action')),
  dedup_key       TEXT,                        -- idempotent re-delivery
  ttl_s           INTEGER,                     -- stale events never wake anyone
  target_hint     TEXT,
  summary         TEXT NOT NULL DEFAULT '',
  body_json       TEXT NOT NULL DEFAULT '{}',
  processed_at    TEXT,                        -- NULL = pending router queue
  delivered_steer TEXT                         -- audit: 'steer:<ok|undispatched>', 'log-only', 'expired', 'unrouted'
);

-- dedup_key is unique forever: re-POSTing an old alert is always a no-op.
CREATE UNIQUE INDEX IF NOT EXISTS idx_stimulus_events_dedup
  ON stimulus_events(dedup_key) WHERE dedup_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_stimulus_events_pending
  ON stimulus_events(processed_at) WHERE processed_at IS NULL;

CREATE TABLE IF NOT EXISTS stimulus_routes (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  source         TEXT NOT NULL,                -- 'cryptobro' | '*'
  type_pattern   TEXT NOT NULL DEFAULT '*',    -- glob: 'signal.*'
  level          TEXT NOT NULL DEFAULT 'action' CHECK (level IN ('info', 'action', '*')),
  target_session TEXT,                         -- NULL = log-only
  enabled        INTEGER NOT NULL DEFAULT 1,
  priority       INTEGER NOT NULL DEFAULT 0,   -- first enabled match wins
  note           TEXT,
  created_at     TEXT NOT NULL,
  created_by     TEXT
);

-- Seed route #1: cryptobro action signals -> the Crypto Bob channel.
-- Seeded DISABLED: rollout phase 1 ships the spine inert (docs plan, Part 3);
-- flipping enabled=1 is the phase-3 switch, after the phase-2 shadow review.
INSERT INTO stimulus_routes
  (source, type_pattern, level, target_session, enabled, priority, note,
   created_at, created_by)
VALUES
  ('cryptobro', 'signal.*', 'action',
   'agent:main:whatsapp:group:120363410716086644',
   0, 0, 'Crypto Bob channel — cryptobro action signals (seeded disabled)',
   datetime('now'), 'migration-002');
