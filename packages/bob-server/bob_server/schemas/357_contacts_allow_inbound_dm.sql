-- Split "exists in the contact list" from "may open a WhatsApp DM session".
--
-- The inbound DM gate previously keyed on bare contact existence, which made
-- every directory entry a potential entry point. Agent-created contacts
-- (create_contact tool, for outbound calls) are written with
-- allow_inbound_dm = 0: present for calls/search/memory, but their number
-- cannot open a DM session. Existing rows default to 1 — behaviour for every
-- current human contact is unchanged.

ALTER TABLE contacts ADD COLUMN allow_inbound_dm INTEGER NOT NULL DEFAULT 1;
