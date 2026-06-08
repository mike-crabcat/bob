-- Store the memory entity ID for each WhatsApp group.
-- Set when ensure_group_entity creates the corresponding group entity.

ALTER TABLE whatsappgroups ADD COLUMN memory_entity_id TEXT;
