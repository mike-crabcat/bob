-- Out-of-channel outreach detector (2026-08-26 review of the David/coffee
-- incident): an outreach goal's target answered in a shared group instead of
-- the outreach DM, and no path carried that to the goal. This sweep probes
-- inbound messages from a contact with an active outreach goal elsewhere;
-- the probe log is the per-(goal, message) idempotency key and the
-- observability record; the watermark replays message.received events the
-- same way the claim router replays claims_created.

CREATE TABLE outreach_probe_log (
    id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL,
    message_id TEXT NOT NULL,           -- messages.id of the inbound message
    verdict TEXT NOT NULL,              -- satisfied|not_satisfied|error
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(goal_id, message_id)
);
CREATE INDEX idx_outreach_probe_log_goal ON outreach_probe_log (goal_id, created_at);

CREATE TABLE outreach_detector_watermark (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    event_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Effects reaper precision: when a delivery was last claimed, so a crash
-- between the pending→delivering flip and the outcome write can be detected
-- (status='delivering' with a stale claimed_at) without guessing from
-- created_at, which would also catch long-backoff effects claimed recently.
ALTER TABLE effects ADD COLUMN claimed_at TEXT;
