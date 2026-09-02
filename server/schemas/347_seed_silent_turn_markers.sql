-- Seed memory_extraction_turns for every existing session so that enabling
-- silent extraction mode (BOB_MEMORY_EXTRACTION_MODE=silent) does NOT trigger a
-- one-time surge over all historical conversation windows.
--
-- Each existing session is marked "caught up to its last message": ran_at is
-- set to MAX(created_at) for that session, so only messages arriving AFTER this
-- seed will satisfy the "undigested messages since last turn" check and trigger
-- a silent turn. Historical conversations are left to the bulletin system that
-- already extracted from them; silent extraction is forward-only from enablement.
--
-- Idempotent: the NOT EXISTS guard means re-running (or a session that already
-- has a real silent-turn row, e.g. from a prior test) is not double-seeded.

INSERT INTO memory_extraction_turns (id, session_key, message_id, ran_at, claims_created)
SELECT
    'seed-' || sm.session_key,
    sm.session_key,
    'seed',
    MAX(sm.created_at),
    0
FROM session_messages sm
WHERE sm.session_key NOT LIKE 'subagent:%'
  AND NOT EXISTS (
      SELECT 1 FROM memory_extraction_turns met
      WHERE met.session_key = sm.session_key
  )
GROUP BY sm.session_key;
