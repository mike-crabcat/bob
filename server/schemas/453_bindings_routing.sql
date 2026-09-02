-- Session-model cleanup Increment 4 (phase A): bindings absorb routing.
--
-- bindings gain contact_id + is_active so they can answer everything the
-- ~17 session_routes read sites ask (channel, endpoint_kind, address,
-- contact). Route CRUD dual-writes into bindings from this deploy; readers
-- port to ConversationRepository.route_for(). session_routes itself is
-- dropped in a later deploy after a read-only window.
--
-- memory_verbose (/verbose) also moves to conversations.policy_json — it
-- was the last config flag living in route metadata.

ALTER TABLE bindings ADD COLUMN contact_id TEXT;
ALTER TABLE bindings ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1;

UPDATE bindings SET contact_id = (
    SELECT r.contact_id FROM session_routes r
    WHERE r.session_key = bindings.session_key
      AND r.deleted_at IS NULL AND r.contact_id IS NOT NULL
    ORDER BY r.updated_at DESC LIMIT 1)
WHERE contact_id IS NULL;

UPDATE bindings SET is_active = 0
WHERE EXISTS (
    SELECT 1 FROM session_routes r
    WHERE r.session_key = bindings.session_key)
  AND NOT EXISTS (
    SELECT 1 FROM session_routes r
    WHERE r.session_key = bindings.session_key
      AND r.deleted_at IS NULL AND r.is_active = 1);

UPDATE conversations SET policy_json = json_patch(
    COALESCE(policy_json, '{}'),
    (SELECT json_object('memory_verbose', json_extract(r.metadata, '$.memory_verbose'))
     FROM session_routes r
     WHERE r.session_key = conversations.id
       AND r.deleted_at IS NULL AND r.is_active = 1
     ORDER BY r.updated_at DESC LIMIT 1))
WHERE EXISTS (
    SELECT 1 FROM session_routes r
    WHERE r.session_key = conversations.id
      AND r.deleted_at IS NULL AND r.is_active = 1
      AND json_extract(r.metadata, '$.memory_verbose') IS NOT NULL);
