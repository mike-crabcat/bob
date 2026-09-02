-- Claim type registry: predefined types that constrain extraction.
-- Each type has a snake_case key, applicable entity types, description, and example.

CREATE TABLE IF NOT EXISTS memory_claim_types (
    key TEXT PRIMARY KEY,
    applicable_types TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    example TEXT NOT NULL DEFAULT ''
);

-- ---------------------------------------------------------------------------
-- Contact claims
-- ---------------------------------------------------------------------------
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('alias', '["contact","group","location"]', 'Alternative name or nickname', 'contact-7f3a91 -> Cleaver');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('appearance', '["contact"]', 'Physical description', 'contact-7f3a91 -> tall, short brown hair, glasses');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('spouse', '["contact"]', 'Spouse or partner', 'contact-7f3a91 -> contact-abc123');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('parent', '["contact"]', 'Parent of this person', 'contact-7f3a91 -> contact-def456');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('child', '["contact"]', 'Child of this person', 'contact-7f3a91 -> contact-ghi789');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('sibling', '["contact"]', 'Brother or sister', 'contact-7f3a91 -> contact-jkl012');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('grandparent', '["contact"]', 'Grandparent of this person', 'contact-7f3a91 -> contact-mno345');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('grandchild', '["contact"]', 'Grandchild of this person', 'contact-7f3a91 -> contact-pqr678');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('social_relation', '["contact"]', 'Non-family connection to another contact (friend, colleague, mentor, neighbor)', 'contact-7f3a91 -> contact-stu901 (colleague)');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('home_address', '["contact"]', 'Where they live', 'contact-7f3a91 -> 42 Bondi Rd, Sydney');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('workplace', '["contact"]', 'Where they work', 'contact-7f3a91 -> Google, Sydney office');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('job', '["contact"]', 'What they do for work', 'contact-7f3a91 -> Software Engineer');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('food_preference', '["contact"]', 'Food likes and dislikes', 'contact-7f3a91 -> loves Thai food, hates coriander');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('drink_preference', '["contact"]', 'Drink likes and dislikes', 'contact-7f3a91 -> prefers red wine, no beer');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('dietary_restriction', '["contact"]', 'Dietary needs, allergies, restrictions', 'contact-7f3a91 -> celiac, shellfish allergy');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('interest', '["contact"]', 'Hobbies, passions, activities', 'contact-7f3a91 -> surfing, photography');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('personality', '["contact"]', 'Temperament and character traits', 'contact-7f3a91 -> easygoing, punctual');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('language', '["contact"]', 'Languages spoken', 'contact-7f3a91 -> English, conversational Indonesian');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('birthday', '["contact"]', 'Date of birth', 'contact-7f3a91 -> 1990-03-15');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('contact_method', '["contact"]', 'Phone, email, social handle', 'contact-7f3a91 -> email: mike@example.com');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('hometown', '["contact"]', 'Where they grew up', 'contact-7f3a91 -> Melbourne');

-- ---------------------------------------------------------------------------
-- Group claims
-- ---------------------------------------------------------------------------
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('purpose', '["group","event","trip","artifact","task"]', 'What this entity is for or why it exists', 'group-bali-gang -> planning the family Bali trip');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('vibe', '["group"]', 'How people act in the group, how you are treated', 'group-bali-gang -> casual, lots of banter, everyone chips in');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('member', '["group","trip"]', 'Person who belongs to this group or trip', 'group-bali-gang -> contact-7f3a91');

-- ---------------------------------------------------------------------------
-- Event claims
-- ---------------------------------------------------------------------------
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('name', '["event","artifact","task"]', 'Name or title of the entity', 'event-dinner-aug5 -> Dinner at Mama San');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('start_time', '["event","transport"]', 'When it starts or departs', 'event-dinner-aug5 -> 2026-08-05T19:00');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('end_time', '["event"]', 'When it ends', 'event-dinner-aug5 -> 2026-08-05T22:00');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('location', '["event"]', 'Where it takes place', 'event-dinner-aug5 -> location-mama-san');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('organizer', '["event"]', 'Who is running or hosting it', 'event-dinner-aug5 -> contact-7f3a91');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('attendee', '["event"]', 'Who is attending', 'event-dinner-aug5 -> contact-abc123');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('recurrence', '["event"]', 'Recurring pattern or one-off', 'event-dinner-aug5 -> one-off');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('associated_trip', '["event"]', 'Trip this event relates to', 'event-dinner-aug5 -> trip-bali-2026');

-- ---------------------------------------------------------------------------
-- Location claims
-- ---------------------------------------------------------------------------
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('location_type', '["location"]', 'Kind of place: venue, house, city, region, restaurant', 'location-villa-sunset -> villa');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('parent_location', '["location"]', 'Location this is contained within', 'location-villa-sunset -> location-seminyak');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('address', '["location"]', 'Street address or directions', 'location-villa-sunset -> Jl. Kayu Aya No. 50, Seminyak');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('associated_contact', '["location"]', 'Person who lives there or owns it', 'location-mike-house -> contact-7f3a91');

-- ---------------------------------------------------------------------------
-- Trip claims
-- ---------------------------------------------------------------------------
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('destination', '["trip"]', 'Overall destination of the trip', 'trip-bali-2026 -> Bali, Indonesia');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('start_date', '["trip"]', 'When the trip begins', 'trip-bali-2026 -> 2026-08-01');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('end_date', '["trip"]', 'When the trip ends', 'trip-bali-2026 -> 2026-08-10');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('stop', '["trip"]', 'A TripStop that is part of this trip', 'trip-bali-2026 -> tripstop-bali-day1-3');

-- ---------------------------------------------------------------------------
-- TripStop claims
-- ---------------------------------------------------------------------------
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('transport_to', '["tripstop"]', 'Transport entity for getting there', 'tripstop-bali-day1-3 -> transport-flight-qz541');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('transport_from', '["tripstop"]', 'Transport entity for leaving', 'tripstop-bali-day1-3 -> transport-driver-ubud');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('stay', '["tripstop"]', 'Location where you stay', 'tripstop-bali-day1-3 -> location-villa-sunset');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('arrival', '["tripstop"]', 'Date/time of arrival', 'tripstop-bali-day1-3 -> 2026-08-01T14:00');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('departure', '["tripstop"]', 'Date/time of departure', 'tripstop-bali-day1-3 -> 2026-08-03T10:00');

-- ---------------------------------------------------------------------------
-- Transport claims
-- ---------------------------------------------------------------------------
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('transport_type', '["transport"]', 'Kind of transport: plane, car, train, bus, boat, taxi', 'transport-flight-qz541 -> plane');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('departure_time', '["transport"]', 'When it leaves', 'transport-flight-qz541 -> 2026-08-01T06:00');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('duration', '["transport"]', 'How long the journey takes', 'transport-flight-qz541 -> 6 hours');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('departure_location', '["transport"]', 'Where it departs from', 'transport-flight-qz541 -> location-sydney-airport');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('arrival_location', '["transport"]', 'Where it arrives at', 'transport-flight-qz541 -> location-dps-airport');

-- ---------------------------------------------------------------------------
-- Task claims
-- ---------------------------------------------------------------------------
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('owner', '["task","artifact"]', 'Person responsible for or maintaining it', 'task-book-villa -> contact-7f3a91');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('due_date', '["task"]', 'Deadline', 'task-book-villa -> 2026-07-01');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('description', '["task"]', 'What needs doing', 'task-book-villa -> Compare 3 villa options and book');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('task_status', '["task"]', 'Current status: open, in-progress, done, blocked', 'task-book-villa -> in-progress');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('related_entity', '["task","decision","artifact"]', 'Another entity this belongs to or references', 'task-book-villa -> trip-bali-2026');

-- ---------------------------------------------------------------------------
-- Artifact claims
-- ---------------------------------------------------------------------------
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('file_path', '["artifact"]', 'Where the file lives', 'artifact-villa-spreadsheet -> https://docs.google.com/spreadsheets/d/abc');

-- ---------------------------------------------------------------------------
-- Decision claims
-- ---------------------------------------------------------------------------
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('decider', '["decision"]', 'Who made the decision', 'decision-stay-seminyak -> contact-7f3a91');
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('rationale', '["decision"]', 'Why this decision was made', 'decision-stay-seminyak -> Close to restaurants and beach, good for kids');

-- ---------------------------------------------------------------------------
-- Cross-cutting claim types (applicable to any entity)
-- ---------------------------------------------------------------------------
INSERT INTO memory_claim_types (key, applicable_types, description, example) VALUES ('artifact_ref', '["contact","group","location","trip","tripstop","transport","event","task","artifact","decision"]', 'Relevant artifact attached to this entity', 'trip-bali-2026 -> artifact-villa-spreadsheet');
