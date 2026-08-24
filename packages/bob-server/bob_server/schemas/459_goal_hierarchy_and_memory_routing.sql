-- Bob Events Phases 1–2 (bob-events-plan.md §1.1, §2.1, §2.3):
-- goal hierarchy + goal↔conversation holders, and the memory→goal routing
-- tables (entity-mention index, router decision log, replay watermark).
--
-- goal_conversations.conversation_id holds CANONICAL conversation ids
-- (resolve_cid()); never raw session_keys — bindings can merge, and the
-- router's echo-suppression breaks if one conversation appears under two keys.

ALTER TABLE goals ADD COLUMN parent_goal_id TEXT REFERENCES goals(id);
CREATE INDEX idx_goals_parent ON goals (parent_goal_id, status);

CREATE TABLE goal_conversations (
    goal_id TEXT NOT NULL REFERENCES goals(id),
    conversation_id TEXT NOT NULL,        -- canonical cid via resolve_cid()
    role TEXT NOT NULL DEFAULT 'holder',  -- holder|origin|worker
    created_at TEXT NOT NULL,
    PRIMARY KEY (goal_id, conversation_id)
);
CREATE INDEX idx_goal_conversations_cid ON goal_conversations (conversation_id);

-- Entity↔conversation interval index (plan §2.1): which conversations
-- discussed which entity, and over what message range. Maintained by
-- write_claim (single chokepoint) + the backfill script.
CREATE TABLE memory_entity_mentions (
    entity_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,        -- canonical cid
    first_message_id TEXT NOT NULL,
    last_message_id TEXT NOT NULL,
    first_at TEXT NOT NULL,
    last_at TEXT NOT NULL,
    PRIMARY KEY (entity_id, conversation_id)
);
CREATE INDEX idx_memory_entity_mentions_conv ON memory_entity_mentions (conversation_id);

-- Router decisions (plan §2.3) — the routing analogue of attention_shadow.
CREATE TABLE memory_routing_log (
    id TEXT PRIMARY KEY,
    stimulus_id TEXT NOT NULL,            -- e.g. the extraction turn message id
    source_conversation_id TEXT NOT NULL,
    goal_id TEXT NOT NULL,
    claim_ids TEXT NOT NULL DEFAULT '[]',  -- JSON array
    entity_ids TEXT NOT NULL DEFAULT '[]', -- JSON array
    match_type TEXT NOT NULL,             -- ref|mention|participant
    probe_verdict TEXT,                   -- relevant|ignore|skipped|error
    revise_outcome TEXT,                  -- revised|no_change|skipped|error
    wake_decision TEXT,                   -- wake|no_wake|shadow_wake
    detail TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_memory_routing_log_goal ON memory_routing_log (goal_id, created_at);
CREATE INDEX idx_memory_routing_log_source ON memory_routing_log (source_conversation_id, created_at);

-- Durable routing replay watermark (plan §2.2): the last memory.claims_created
-- event id whose routing effects are durably enqueued. Single row.
CREATE TABLE claim_router_watermark (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    event_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
