-- Bob3 core schema (plan Phase I).
-- Append-only event log + mutable processing state (turns/effects/wakeups).
-- SQL kept ANSI/Postgres-portable: TEXT ids, TEXT timestamps (ISO-8601 UTC),
-- INTEGER counters, no SQLite-only column types.

-- The single durable record of every accepted stimulus. Append-only:
-- rows are never updated or deleted (retention decision 7: kept forever;
-- deletions are modeled as tombstone events).
CREATE TABLE IF NOT EXISTS event_log (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    binding_key TEXT NOT NULL,          -- immutable channel address at ingestion (invariant 3)
    conversation_id TEXT NOT NULL,      -- conversation assigned at ingestion (invariant 3)
    source TEXT NOT NULL,               -- e.g. 'whatsapp', 'email', 'phone', 'heartbeat', 'cron'
    external_id TEXT,                   -- source-native id; uniqueness key when present
    causation_id TEXT,                  -- event id that directly caused this one
    correlation_id TEXT,                -- episode/goal thread id
    occurred_at TEXT NOT NULL,          -- source-claimed time
    recorded_at TEXT NOT NULL,          -- ingestion time
    payload_json TEXT NOT NULL DEFAULT '{}'
);

-- Invariant 1: a stimulus is accepted at most once per (source, external_id).
CREATE UNIQUE INDEX IF NOT EXISTS idx_event_log_source_external
    ON event_log (source, external_id) WHERE external_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_event_log_conversation
    ON event_log (conversation_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_event_log_type_recorded
    ON event_log (event_type, recorded_at);

-- Turn execution state (invariants 4-6). Mutable; never in the log.
CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending|running|succeeded|failed|dead
    input_high_watermark TEXT,               -- max event id fixed before execution (invariant 5)
    lease_owner TEXT,
    lease_expires_at TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    completed_at TEXT,
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_turns_conversation_status
    ON turns (conversation_id, status);
-- Invariant 4: at most one active lease per conversation.
CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_one_active_per_conversation
    ON turns (conversation_id) WHERE status IN ('pending', 'running');

-- Immutable claims: which events a turn consumed (invariant 5/6).
CREATE TABLE IF NOT EXISTS turn_events (
    turn_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    PRIMARY KEY (turn_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_turn_events_event ON turn_events (event_id);

-- Effects outbox (invariant 7): every external action is durable before
-- delivery and idempotent under retry.
CREATE TABLE IF NOT EXISTS effects (
    id TEXT PRIMARY KEY,
    turn_id TEXT,
    kind TEXT NOT NULL,                      -- e.g. 'whatsapp_send', 'email_send', 'call_place'
    idempotency_key TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',  -- pending|delivering|delivered|failed|dead
    attempt INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,              -- retry backoff gate
    delivered_at TEXT,
    external_result_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_effects_idempotency
    ON effects (idempotency_key);
CREATE INDEX IF NOT EXISTS idx_effects_status_available
    ON effects (status, available_at);

-- Scheduled wakeups: cancellable, reschedulable (heartbeat/cron/deadline
-- work moves here in Phase III).
CREATE TABLE IF NOT EXISTS wakeups (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    goal_id TEXT,
    not_before TEXT NOT NULL,
    recurrence TEXT,                         -- cron-ish spec, NULL = one-shot
    tz TEXT,
    status TEXT NOT NULL DEFAULT 'scheduled', -- scheduled|fired|cancelled
    created_by_turn TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wakeups_due
    ON wakeups (status, not_before);

-- Structural trigger patterns for ambient stimuli (plan v2: subscriptions).
CREATE TABLE IF NOT EXISTS subscriptions (
    id TEXT PRIMARY KEY,
    pattern_json TEXT NOT NULL,              -- exact-match structural pattern (v1)
    conversation_id TEXT NOT NULL,
    goal_id TEXT,
    created_by_turn TEXT,
    expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'active',   -- active|expired|cancelled
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_active
    ON subscriptions (status, expires_at);
