-- Add assigned_identity claim type: playful identities/epithets a group has
-- settled on for a person (or for Bob, on self-bob) — standing nicknames,
-- running-bit personas. Must mirror the registry entry in
-- services/memory/claim_types.py.

INSERT OR IGNORE INTO memory_claim_types (key, applicable_types, description, example) VALUES (
    'assigned_identity',
    '["person","self"]',
    'Playful identity or epithet a GROUP has settled on for this person (or for Bob, on self-bob) — standing nicknames, running-bit personas (e.g. ''the Optimus Prime of the group'', ''the GPT-2 of AI Doom'', ''team mum''). Durable group lore, not a self-stated fact: attribute to the person it is assigned TO and name the assigning group in the value. Only record once the group has settled on it — not a first-pass suggestion.',
    'person-mike-cleaver → "Optimus Prime of the Leeming Boys (group-leeming-boys) — 50% more buff than canon"'
);
