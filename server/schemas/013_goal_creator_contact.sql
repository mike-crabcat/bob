-- Goal creator pinning (2026-09-25). Goals record conversations, not people;
-- "who commissioned this" was only derivable from the origin conversation,
-- which is ambiguous for group-raised goals (mug-incident requester
-- misresolution, one level up). creator_contact_id is pinned AT CREATION:
-- DM-commissioned = the DM contact; group-commissioned = the named owner
-- (Bob asks "who owns this?" per Mike's rule); children inherit from the
-- parent; system/dream goals stay NULL (scoped down, never trusted).
-- It drives capability scoping in the goal room (group_participants etc.)
-- with the creator's own trust — a member's goal sees only what the member
-- could see.
ALTER TABLE goals ADD COLUMN creator_contact_id TEXT REFERENCES contacts(id);

-- Backfill: derive creator for existing goals whose origin is a 1:1 DM.
-- Phone match is digit-normalised (binding addresses vary: +614…, bare,
-- @s.whatsapp.net suffixed). Group/system origins intentionally stay NULL.
UPDATE goals
SET creator_contact_id = (
    SELECT ct.id
    FROM bindings b
    JOIN conversations cv ON cv.id = b.conversation_id AND cv.kind = 'dm'
    JOIN contacts ct ON ct.deleted_at IS NULL
        AND replace(replace(ct.phone_number, '+', ''), ' ', '') =
            replace(replace(replace(b.address, '+', ''), ' ', ''),
                    '@s.whatsapp.net', '')
    WHERE b.conversation_id = goals.origin_conversation_id
      AND b.channel = 'whatsapp'
    LIMIT 1)
WHERE creator_contact_id IS NULL
  AND origin_conversation_id IS NOT NULL;
