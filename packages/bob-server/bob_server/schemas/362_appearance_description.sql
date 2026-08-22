-- Tighten the appearance claim type to durable physical descriptions of a
-- person only (no photo captions, scheduling, or instructions).
-- Must mirror the registry entry in services/memory/claim_types.py.

UPDATE memory_claim_types
SET description = 'Durable physical description of the person — build, complexion, hair and facial hair, eyes, habitual accessories (glasses), distinguishing features. NOT a photo caption: clothing/props/background of one image only as ''in this photo…''; NOT scheduling or attendance; NOT instructions. One canonical description per person — an update replaces the previous wording.',
    example = 'person-mike-cleaver → "medium-solid build, short brown hair greying at the sides, full beard, rectangular glasses"'
WHERE key = 'appearance';
