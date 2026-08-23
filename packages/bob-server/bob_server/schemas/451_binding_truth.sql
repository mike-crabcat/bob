-- Session-model cleanup Increment 2: make bindings truthful.
--
-- endpoint_kind (dm|group|thread|call) is a NEW column — the existing
-- `kind` column means identity|thread (430) and is not overloaded.
-- address is backfilled from the routing truth we already hold:
--   groups/threads -> session_routes.chat_id (WhatsApp JID / email thread id)
--   DMs            -> route.contact_id -> contacts (phone/email)
-- Write paths (whatsapp ingress, email polling/delivery, voice dispatch)
-- pass these at ensure()/bind() time from this deploy on; the key-tail
-- parse remains only as a read-time fallback for rows predating routes.

ALTER TABLE bindings ADD COLUMN endpoint_kind TEXT;

-- endpoint_kind from the most recent active route for the binding key.
UPDATE bindings SET endpoint_kind = (
    SELECT r.kind FROM session_routes r
    WHERE r.session_key = bindings.session_key AND r.deleted_at IS NULL
    ORDER BY r.updated_at DESC LIMIT 1)
WHERE endpoint_kind IS NULL;

-- Fallback: derive from key shape / channel for rows without a route.
UPDATE bindings SET endpoint_kind = CASE
    WHEN session_key LIKE '%:group:%' THEN 'group'
    WHEN session_key LIKE '%:dm:%' THEN 'dm'
    WHEN channel = 'voice' THEN 'call'
    ELSE 'thread'
END
WHERE endpoint_kind IS NULL;

-- Address 1/2: wire address from route chat_id (groups, email threads,
-- and the WhatsApp DMs that carry a JID).
UPDATE bindings SET address = (
    SELECT r.chat_id FROM session_routes r
    WHERE r.session_key = bindings.session_key
      AND r.deleted_at IS NULL AND r.chat_id IS NOT NULL
    ORDER BY r.updated_at DESC LIMIT 1)
WHERE address IS NULL;

-- Address 2/2: DM address via the route's contact (phone, else email).
UPDATE bindings SET address = (
    SELECT COALESCE(c.phone_number, c.email)
    FROM session_routes r JOIN contacts c ON c.id = r.contact_id
    WHERE r.session_key = bindings.session_key
      AND r.deleted_at IS NULL AND r.contact_id IS NOT NULL
    ORDER BY r.updated_at DESC LIMIT 1)
WHERE address IS NULL;
