-- Fold WhatsApp linked-device DM variants into their base conversation.
--
-- Ingress derived DM session keys from the raw sender JID, so messages from
-- a linked device (…dm:<phone>:<device>, e.g. WhatsApp Web) landed in a
-- separate conversation from the same person's primary phone (…dm:<phone>)
-- — split history, split per-conversation policy (model_override,
-- patience), duplicate bindings. Ingress now strips the device suffix (see
-- whatsapp_bridge_service/_service.py); this folds the variants that
-- accumulated: messages, bindings, goals, goal holders and participants
-- move to the base conversation, and the variant rows are marked
-- merged_into (the conversations.merge() shape — that repo method exists
-- but has no caller; SQL migration is the one-time bulk form).
--
-- A variant is: 'agent:main:whatsapp:dm:%' with exactly 5 colons (the base
-- has 4), not already merged, whose rtrim-derived base (strip the trailing
-- :<device> digits) exists as a conversation.

UPDATE messages
SET conversation_id = rtrim(rtrim(conversation_id, '0123456789'), ':')
WHERE conversation_id IN (
    SELECT id FROM conversations
    WHERE id LIKE 'agent:main:whatsapp:dm:%'
      AND length(id) - length(replace(id, ':', '')) = 5
      AND merged_into IS NULL
      AND EXISTS (SELECT 1 FROM conversations b
                  WHERE b.id = rtrim(rtrim(conversations.id, '0123456789'), ':')
                    AND b.id != conversations.id));

UPDATE bindings
SET conversation_id = rtrim(rtrim(conversation_id, '0123456789'), ':'),
    merged_from = conversation_id,
    merged_at = datetime('now')
WHERE conversation_id IN (
    SELECT id FROM conversations
    WHERE id LIKE 'agent:main:whatsapp:dm:%'
      AND length(id) - length(replace(id, ':', '')) = 5
      AND merged_into IS NULL
      AND EXISTS (SELECT 1 FROM conversations b
                  WHERE b.id = rtrim(rtrim(conversations.id, '0123456789'), ':')
                    AND b.id != conversations.id));

UPDATE goals
SET conversation_id = rtrim(rtrim(conversation_id, '0123456789'), ':')
WHERE conversation_id IN (
    SELECT id FROM conversations
    WHERE id LIKE 'agent:main:whatsapp:dm:%'
      AND length(id) - length(replace(id, ':', '')) = 5
      AND merged_into IS NULL
      AND EXISTS (SELECT 1 FROM conversations b
                  WHERE b.id = rtrim(rtrim(conversations.id, '0123456789'), ':')
                    AND b.id != conversations.id));

-- goal_conversations / participants have composite PKs anchored on
-- conversation_id: a variant row whose key-twin already exists on the base
-- would violate uniqueness. Colliding variant rows are redundant (the base
-- row represents the same holder/participant) — delete those, move the rest.

DELETE FROM goal_conversations
WHERE conversation_id IN (
    SELECT id FROM conversations
    WHERE id LIKE 'agent:main:whatsapp:dm:%'
      AND length(id) - length(replace(id, ':', '')) = 5
      AND merged_into IS NULL
      AND EXISTS (SELECT 1 FROM conversations b
                  WHERE b.id = rtrim(rtrim(conversations.id, '0123456789'), ':')
                    AND b.id != conversations.id))
  AND EXISTS (SELECT 1 FROM goal_conversations g2
              WHERE g2.conversation_id =
                    rtrim(rtrim(goal_conversations.conversation_id, '0123456789'), ':')
                AND g2.goal_id = goal_conversations.goal_id);

UPDATE goal_conversations
SET conversation_id = rtrim(rtrim(conversation_id, '0123456789'), ':')
WHERE conversation_id IN (
    SELECT id FROM conversations
    WHERE id LIKE 'agent:main:whatsapp:dm:%'
      AND length(id) - length(replace(id, ':', '')) = 5
      AND merged_into IS NULL
      AND EXISTS (SELECT 1 FROM conversations b
                  WHERE b.id = rtrim(rtrim(conversations.id, '0123456789'), ':')
                    AND b.id != conversations.id));

DELETE FROM participants
WHERE conversation_id IN (
    SELECT id FROM conversations
    WHERE id LIKE 'agent:main:whatsapp:dm:%'
      AND length(id) - length(replace(id, ':', '')) = 5
      AND merged_into IS NULL
      AND EXISTS (SELECT 1 FROM conversations b
                  WHERE b.id = rtrim(rtrim(conversations.id, '0123456789'), ':')
                    AND b.id != conversations.id))
  AND EXISTS (SELECT 1 FROM participants p2
              WHERE p2.conversation_id =
                    rtrim(rtrim(participants.conversation_id, '0123456789'), ':')
                AND p2.identifier = participants.identifier);

UPDATE participants
SET conversation_id = rtrim(rtrim(conversation_id, '0123456789'), ':')
WHERE conversation_id IN (
    SELECT id FROM conversations
    WHERE id LIKE 'agent:main:whatsapp:dm:%'
      AND length(id) - length(replace(id, ':', '')) = 5
      AND merged_into IS NULL
      AND EXISTS (SELECT 1 FROM conversations b
                  WHERE b.id = rtrim(rtrim(conversations.id, '0123456789'), ':')
                    AND b.id != conversations.id));

-- Mark last, after the moves above (their guards read merged_into IS NULL).
UPDATE conversations
SET merged_into = rtrim(rtrim(id, '0123456789'), ':'),
    updated_at = datetime('now')
WHERE id LIKE 'agent:main:whatsapp:dm:%'
  AND length(id) - length(replace(id, ':', '')) = 5
  AND merged_into IS NULL
  AND EXISTS (SELECT 1 FROM conversations b
              WHERE b.id = rtrim(rtrim(conversations.id, '0123456789'), ':')
                AND b.id != conversations.id);
