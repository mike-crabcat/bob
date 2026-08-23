-- Session-model cleanup Increment 3: config out of route metadata.
--
-- Attention flags (patience_*) and dream autoplan move to
-- conversations.policy_json — they gate the conversation's dispatch
-- behaviour. Route metadata copies stay for one deploy as a read
-- fallback, then the fallback is dropped.
--
-- Outreach workflow state moves to the outreach goal (strategy/progress
-- on goals) where its lifecycle already lives. The stale route-metadata
-- copies are stripped here: every outreach_* row in production belongs to
-- an already-completed goal, and leaving them injects a bogus "Active
-- Outreach Request" prompt on every message in those DMs.

UPDATE conversations SET policy_json = json_patch(
    COALESCE(policy_json, '{}'),
    (SELECT json_object(
        'patience_enabled', json_extract(r.metadata, '$.patience_enabled'),
        'patience_relevance_gating', json_extract(r.metadata, '$.patience_relevance_gating'),
        'dream_autoplan', json_extract(r.metadata, '$.dream_autoplan'))
     FROM session_routes r
     WHERE r.session_key = conversations.id
       AND r.deleted_at IS NULL AND r.is_active = 1
     ORDER BY r.updated_at DESC LIMIT 1))
WHERE EXISTS (
    SELECT 1 FROM session_routes r
    WHERE r.session_key = conversations.id
      AND r.deleted_at IS NULL AND r.is_active = 1
      AND (json_extract(r.metadata, '$.patience_enabled') IS NOT NULL
        OR json_extract(r.metadata, '$.patience_relevance_gating') IS NOT NULL
        OR json_extract(r.metadata, '$.dream_autoplan') IS NOT NULL));

UPDATE session_routes SET metadata = json_remove(metadata,
    '$.outreach_initiated_from', '$.outreach_objective',
    '$.outreach_requestor', '$.outreach_message', '$.outreach_goal_id')
WHERE metadata LIKE '%outreach_%';
