-- Allow direction='voice_link' on phone_calls.
--
-- Browser voice-link calls (voice_sessions) now mirror into phone_calls so
-- they appear in the calls UI alongside Twilio calls — they run the same
-- Realtime bridge and lifecycle. SQLite can't ALTER a CHECK constraint, so
-- this is the standard rebuild-and-copy.

CREATE TABLE phone_calls_new (
    id TEXT PRIMARY KEY,
    call_sid TEXT,
    stream_sid TEXT,
    phone_number TEXT,
    direction TEXT NOT NULL DEFAULT 'outbound'
        CHECK (direction IN ('inbound', 'outbound', 'voice_link')),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('ringing', 'active', 'completed', 'failed', 'canceled', 'busy', 'no-answer')),
    agenda TEXT NOT NULL DEFAULT '',
    exchange_count INTEGER NOT NULL DEFAULT 0,
    duration_seconds REAL,
    recording_path TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    origin_session_key TEXT,
    engine TEXT NOT NULL DEFAULT 'default',
    transcript TEXT NOT NULL DEFAULT '',
    realtime_meta TEXT NOT NULL DEFAULT '{}',
    subagent_id TEXT,
    result_dispatched_at TEXT,
    outcome TEXT
);

INSERT INTO phone_calls_new SELECT * FROM phone_calls;
DROP TABLE phone_calls;
ALTER TABLE phone_calls_new RENAME TO phone_calls;

-- Backfill mirrors for existing voice_sessions so history shows up too.
-- Status maps pending→ringing, active→ringing (stale after restarts; the
-- startup sweep will complete any truly-live ones), completed→completed,
-- expired→canceled. Phone number is derived from the origin DM session key
-- when possible.
INSERT INTO phone_calls
    (id, call_sid, phone_number, direction, status, agenda, engine,
     transcript, duration_seconds, outcome, subagent_id, origin_session_key,
     started_at, completed_at)
SELECT
    vs.id, '',
    CASE WHEN vs.origin_session_key LIKE 'agent:main:whatsapp:dm:%'
         THEN '+' || substr(vs.origin_session_key, length('agent:main:whatsapp:dm:') + 1)
         ELSE '' END,
    'voice_link',
    CASE vs.status
        WHEN 'pending' THEN 'ringing'
        WHEN 'active' THEN 'ringing'
        WHEN 'completed' THEN 'completed'
        WHEN 'expired' THEN 'canceled'
        ELSE 'completed' END,
    vs.goal, 'openai_realtime', vs.transcript, vs.duration_seconds, vs.outcome,
    vs.subagent_id, vs.origin_session_key,
    strftime('%Y-%m-%d %H:%M:%S', vs.created_at),
    strftime('%Y-%m-%d %H:%M:%S', vs.completed_at)
FROM voice_sessions vs
WHERE NOT EXISTS (SELECT 1 FROM phone_calls pc WHERE pc.id = vs.id);
