"""Claim type registry and deterministic entity template renderer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ClaimType:
    key: str
    applicable_types: list[str]
    description: str
    example: str


# Hardcoded registry — must match 317_claim_types.sql + 321 migration.
CLAIM_TYPE_REGISTRY: dict[str, ClaimType] = {}

_RAW_TYPES: list[tuple[str, list[str], str, str]] = [
    # Person
    ("alias", ["person", "group", "location"], "Alternative name or nickname (e.g. 'Cleaver', 'Dave'). Not for event status, actions, or phrases", 'person-mike-cleaver → "Cleaver"'),
    ("appearance", ["person"], "Physical description", 'person-mike-cleaver → "tall, short brown hair, glasses"'),
    ("spouse", ["person"], "Spouse or partner", "person-mike-cleaver → person-blair-nicol"),
    ("parent", ["person"], "Parent of this person", "person-mike-cleaver → person-mum"),
    ("child", ["person"], "Child of this person", "person-mike-cleaver → person-bob-jnr"),
    ("sibling", ["person"], "Brother or sister", "person-mike-cleaver → person-sam-parry"),
    ("grandparent", ["person"], "Grandparent of this person", "person-mike-cleaver → person-grandpa"),
    ("grandchild", ["person"], "Grandchild of this person", "person-mike-cleaver → person-new-baby"),
    ("home_address", ["person"], "Where they live", 'person-mike-cleaver → "42 Bondi Rd, Sydney"'),
    ("workplace", ["person"], "Where they work", 'person-mike-cleaver → "Google, Sydney office"'),
    ("job", ["person"], "What they do for work", 'person-mike-cleaver → "Software Engineer"'),
    ("food_preference", ["person"], "Food likes and dislikes", 'person-mike-cleaver → "loves Thai food, hates coriander"'),
    ("drink_preference", ["person"], "Drink likes and dislikes", 'person-mike-cleaver → "prefers red wine, no beer"'),
    ("dietary_restriction", ["person"], "Dietary needs, allergies, restrictions", 'person-mike-cleaver → "celiac, shellfish allergy"'),
    ("interest", ["person"], "Hobbies, passions, activities", 'person-mike-cleaver → "surfing, photography"'),
    ("personality", ["person"], "Temperament and character traits", 'person-mike-cleaver → "easygoing, punctual"'),
    ("language", ["person"], "Languages spoken", 'person-mike-cleaver → "English, conversational Indonesian"'),
    ("birthday", ["person"], "Date of birth", 'person-mike-cleaver → "1990-03-15"'),
    ("contact_method", ["person"], "Phone number, email address, or messaging handle only (e.g. '+61 400 123 456', 'email: mike@example.com', '@handle'). Not for conversation summaries, instructions, or actions", 'person-mike-cleaver → "email: mike@example.com"'),
    ("hometown", ["person"], "Where they grew up", 'person-mike-cleaver → "Melbourne"'),
    ("contact_id", ["person"], "Links to a contacts table row (value = hex8 ID)", "person-mike-cleaver → 7c9f0fd7"),
    # Group
    ("purpose", ["group", "event", "trip", "file", "thing", "task"], "What this entity is for", 'group-bali-gang → "planning the family Bali trip"'),
    ("vibe", ["group"], "How people act in the group", 'group-bali-gang → "casual, lots of banter"'),
    ("member", ["group", "trip"], "Person who belongs to this group or trip", "group-bali-gang → person-mike-cleaver"),
    # Event
    ("name", ["event", "file", "thing", "task"], "Name or title", 'event-dinner-aug5 → "Dinner at Mama San"'),
    ("start_time", ["event", "transport"], "When it starts or departs", 'event-dinner-aug5 → "2026-08-05T19:00"'),
    ("end_time", ["event"], "When it ends", 'event-dinner-aug5 → "2026-08-05T22:00"'),
    ("location", ["event"], "Where it takes place", "event-dinner-aug5 → location-mama-san"),
    ("organizer", ["event"], "Who is running or hosting it", "event-dinner-aug5 → person-mike-cleaver"),
    ("attendee", ["event"], "Who is attending", "event-dinner-aug5 → person-david-shedden"),
    ("recurrence", ["event"], "Recurring pattern or one-off", 'event-dinner-aug5 → "one-off"'),
    ("associated_trip", ["event"], "Trip this event relates to", "event-dinner-aug5 → trip-bali-2026"),
    # Location
    ("location_type", ["location"], "Kind of place", 'location-villa-sunset → "villa"'),
    ("parent_location", ["location"], "Location this is contained within", "location-villa-sunset → location-seminyak"),
    ("address", ["location"], "Street address or directions", 'location-villa-sunset → "Jl. Kayu Aya No. 50"'),
    ("associated_contact", ["location"], "Person who lives there or owns it", "location-mike-house → person-mike-cleaver"),
    # Trip
    ("stop", ["trip"], "TripStop that is part of this trip", "trip-bali-2026 → tripstop-bali-day1-3"),
    # TripStop
    ("transport_to", ["tripstop"], "Transport for getting there", "tripstop-bali-day1-3 → transport-flight-qz541"),
    ("transport_from", ["tripstop"], "Transport for leaving", "tripstop-bali-day1-3 → transport-driver-ubud"),
    ("stay", ["tripstop"], "Location where you stay", "tripstop-bali-day1-3 → location-villa-sunset"),
    ("arrival", ["tripstop"], "Date/time of arrival", 'tripstop-bali-day1-3 → "2026-08-01T14:00"'),
    ("departure", ["tripstop"], "Date/time of departure", 'tripstop-bali-day1-3 → "2026-08-03T10:00"'),
    # Transport
    ("transport_type", ["transport"], "Kind of transport", 'transport-flight-qz541 → "plane"'),
    ("departure_time", ["transport"], "When it leaves", 'transport-flight-qz541 → "2026-08-01T06:00"'),
    ("duration", ["transport"], "How long the journey takes", 'transport-flight-qz541 → "6 hours"'),
    ("departure_location", ["transport"], "Where it departs from", "transport-flight-qz541 → location-sydney-airport"),
    ("arrival_location", ["transport"], "Where it arrives at", "transport-flight-qz541 → location-dps-airport"),
    # Task
    ("owner", ["task", "file", "thing"], "Person responsible", "task-book-villa → person-mike-cleaver"),
    ("due_date", ["task"], "Deadline", 'task-book-villa → "2026-07-01"'),
    ("description", ["task", "thing"], "What needs doing or what this is", 'task-book-villa → "Compare 3 villa options and book"'),
    ("task_status", ["task"], "Status: open, in-progress, done, blocked", 'task-book-villa → "in-progress"'),
    ("related_entity", ["task", "decision", "file", "thing"], "Entity this belongs to", "task-book-villa → trip-bali-2026"),
    # File
    ("file_path", ["file"], "Where the file lives (workspace path or URL)", 'file-villa-spreadsheet → "https://docs.google.com/..."'),
    # Thing
    ("thing_type", ["thing"], "Kind of physical thing: animal, toy, tool, vehicle, furniture, appliance, food, device", 'thing-ebike → "vehicle"'),
    # Decision
    ("decider", ["decision"], "Who made the decision", "decision-stay-seminyak → person-mike-cleaver"),
    ("rationale", ["decision"], "Why this decision was made", 'decision-stay-seminyak → "Close to restaurants and beach"'),
    # Cross-cutting
    ("file_ref", ["person", "group", "location", "trip", "tripstop", "transport", "event", "task", "file", "thing", "decision"],
     "Links to a file entity (object_id must be a file-* entity ID). Not for notes, actions, or non-file entity references", "trip-bali-2026 → file-villa-spreadsheet"),
    ("truth", ["person", "group", "location", "trip", "tripstop", "transport", "event", "task", "file", "thing", "decision"],
     "User-stated fact, correction, or answer. Ground truth from the user that overrides inference.", 'trip-mike-holiday-june-2026 → "Yes, split Paris into two stops"'),
]

for _key, _types, _desc, _ex in _RAW_TYPES:
    CLAIM_TYPE_REGISTRY[_key] = ClaimType(key=_key, applicable_types=_types, description=_desc, example=_ex)

del _RAW_TYPES

# Claim types where object_id is an entity reference (not a scalar value).
# Used by render_entity_full to decide which claims to expand recursively.
ENTITY_REF_CLAIM_KEYS: frozenset[str] = frozenset({
    "spouse", "parent", "child", "sibling", "grandparent", "grandchild",
    "member", "location", "organizer", "attendee", "associated_trip",
    "parent_location", "associated_contact", "stop",
    "transport_to", "transport_from", "stay",
    "departure_location", "arrival_location",
    "owner", "related_entity", "decider", "file_ref",
})


def get_claim_types_for_entity(entity_type: str) -> list[ClaimType]:
    """Return claim types applicable to a given entity type."""
    return [ct for ct in CLAIM_TYPE_REGISTRY.values() if entity_type in ct.applicable_types]


def get_all_keys() -> set[str]:
    """Return all valid claim type keys."""
    return set(CLAIM_TYPE_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Entity type definitions for template rendering
# ---------------------------------------------------------------------------

ENTITY_TYPES: tuple[str, ...] = (
    "person", "group", "location", "trip", "tripstop",
    "transport", "event", "task", "file", "thing", "decision",
)

# Defines the order and labels for rendering each entity type's claims.
_ENTITY_TEMPLATES: dict[str, list[tuple[str, str]]] = {
    "person": [
        ("alias", "Also known as"),
        ("appearance", "Appearance"),
        ("spouse", "Spouse/Partner"),
        ("parent", "Parent"),
        ("child", "Child"),
        ("sibling", "Sibling"),
        ("grandparent", "Grandparent"),
        ("grandchild", "Grandchild"),
        ("home_address", "Home"),
        ("workplace", "Workplace"),
        ("job", "Job"),
        ("birthday", "Birthday"),
        ("hometown", "Hometown"),
        ("language", "Languages"),
        ("contact_method", "Contact"),
        ("food_preference", "Food"),
        ("drink_preference", "Drinks"),
        ("dietary_restriction", "Dietary"),
        ("interest", "Interests"),
        ("personality", "Personality"),
        ("contact_id", "Contact ID"),
        ("file_ref", "Files"),
        ("truth", "User truth"),
    ],
    "group": [
        ("alias", "Also known as"),
        ("purpose", "Purpose"),
        ("vibe", "Vibe"),
        ("member", "Members"),
        ("file_ref", "Files"),
        ("truth", "User truth"),
    ],
    "event": [
        ("name", "Event"),
        ("start_time", "Starts"),
        ("end_time", "Ends"),
        ("location", "Location"),
        ("organizer", "Organizer"),
        ("attendee", "Attendees"),
        ("recurrence", "Recurrence"),
        ("associated_trip", "Trip"),
        ("file_ref", "Files"),
        ("truth", "User truth"),
    ],
    "location": [
        ("alias", "Also known as"),
        ("location_type", "Type"),
        ("address", "Address"),
        ("parent_location", "Part of"),
        ("associated_contact", "Associated with"),
        ("file_ref", "Files"),
        ("truth", "User truth"),
    ],
    "trip": [
        ("purpose", "Purpose"),
        ("member", "Members"),
        ("stop", "Stops"),
        ("file_ref", "Files"),
        ("truth", "User truth"),
    ],
    "tripstop": [
        ("stay", "Staying at"),
        ("arrival", "Arriving"),
        ("departure", "Departing"),
        ("transport_to", "Getting there"),
        ("transport_from", "Leaving via"),
        ("file_ref", "Files"),
        ("truth", "User truth"),
    ],
    "transport": [
        ("transport_type", "Type"),
        ("departure_time", "Departing"),
        ("duration", "Duration"),
        ("departure_location", "From"),
        ("arrival_location", "To"),
        ("file_ref", "Files"),
        ("truth", "User truth"),
    ],
    "task": [
        ("name", "Task"),
        ("description", "Description"),
        ("owner", "Owner"),
        ("task_status", "Status"),
        ("due_date", "Due"),
        ("related_entity", "Related to"),
        ("file_ref", "Files"),
        ("truth", "User truth"),
    ],
    "file": [
        ("name", "File"),
        ("file_path", "Path"),
        ("purpose", "Purpose"),
        ("owner", "Owner"),
        ("related_entity", "Related to"),
        ("file_ref", "Related files"),
        ("truth", "User truth"),
    ],
    "thing": [
        ("name", "Thing"),
        ("thing_type", "Type"),
        ("description", "Description"),
        ("owner", "Owner"),
        ("location", "Location"),
        ("related_entity", "Related to"),
        ("file_ref", "Files"),
        ("truth", "User truth"),
    ],
    "decision": [
        ("decider", "Decided by"),
        ("rationale", "Rationale"),
        ("related_entity", "About"),
        ("file_ref", "Files"),
        ("truth", "User truth"),
    ],
}

# Entity type descriptions for the extraction prompt glossary.
ENTITY_TYPE_DESCRIPTIONS: dict[str, str] = {
    "person": (
        "A specific, individual human being. Only create person entities for real people "
        "mentioned by name in the bulletin. Do NOT create persons for: bots, AI assistants, "
        "companies, teams, tools, services, concepts, places, dates, phone numbers, or software."
    ),
    "group": (
        "A chat group, team, or named collective of people. e.g. a WhatsApp group, a family chat."
    ),
    "location": (
        "A physical place: a city, a venue, a house, a restaurant, a hotel. "
        "Not abstract locations like 'the cloud' or 'the internet'."
    ),
    "trip": (
        "A planned or completed trip/holiday. Contains tripstops (individual legs/stops within the trip). "
        "The trip-level start_date/end_date cover the overall trip window. "
        "Do NOT set destination on the trip — destinations come from the individual tripstop stay locations."
    ),
    "tripstop": (
        "A stop or leg within a trip. Each tripstop is a segment of the overall trip, "
        "with its own arrival/departure and optionally a stay location. "
        "CRITICAL: each distinct stay (different hotel, different city, or different date range "
        "at the same city) MUST be its own separate tripstop entity. Two nights in different "
        "hotels within Paris are two tripstops, not one. Include location and date range in the "
        "slug for uniqueness (e.g. tripstop-paris-june12-14, tripstop-paris-june14-16)."
    ),
    "transport": (
        "A transport leg: a flight, a drive, a train ride, a boat trip. "
        "Has departure and arrival locations and times."
    ),
    "event": (
        "A planned event: a dinner, a party, a meeting, a concert. "
        "Has a time, a location, and attendees."
    ),
    "task": (
        "A task or todo item. Something that needs to be done. "
        "Has an owner, a status, and optionally a due date."
    ),
    "file": (
        "A file or document in the workspace or accessible via URL. "
        "Every file entity MUST have a file_path claim with the actual workspace-relative "
        "path (e.g. 'docs/itinerary.md', 'src/main.py') or a full URL (https://...). "
        "Vague values like 'workspace', 'project', or 'root' are NOT valid file paths. "
        "Do NOT create file entities for things that are not actual files."
    ),
    "thing": (
        "A tangible physical object, animal, or product. "
        "Must have a thing_type claim (animal, tool, vehicle, toy, device, furniture, food, appliance, etc.). "
        "Not for abstract concepts, software, or services."
    ),
    "decision": (
        "A decision that was made. Has a decider (person) and a rationale. "
        "Should reference the entity it's about via related_entity."
    ),
}


def render_entity(
    entity_type: str,
    display_name: str,
    claims: list[dict[str, Any]],
) -> str:
    """Render entity claims into a human-readable text block using templates.

    claims: list of dicts with keys: claim_type_key, object_id, value
    """
    by_type: dict[str, list[str]] = {}
    for claim in claims:
        key = claim["claim_type_key"]
        val = claim.get("value") or claim.get("object_id") or ""
        by_type.setdefault(key, []).append(val)

    template = _ENTITY_TEMPLATES.get(entity_type, [])
    lines: list[str] = [display_name]

    for claim_key, label in template:
        values = by_type.get(claim_key)
        if not values:
            continue
        if len(values) == 1:
            lines.append(f"{label}: {values[0]}")
        else:
            lines.append(f"{label}:")
            for v in values:
                lines.append(f"  - {v}")

    # Append orphan claims (claim types not in the template)
    template_keys = {ck for ck, _ in template}
    orphan_keys = [k for k in by_type if k not in template_keys]
    if orphan_keys:
        lines.append("Orphan claims:")
        for k in sorted(orphan_keys):
            vals = by_type[k]
            if len(vals) == 1:
                lines.append(f"  {k}: {vals[0]}")
            else:
                lines.append(f"  {k}:")
                for v in vals:
                    lines.append(f"    - {v}")

    return "\n".join(lines)


def build_extraction_prompt_section(entity_types: list[str]) -> str:
    """Build the entity type glossary and claim types section for the extraction prompt.

    Groups claim types under their entity types with descriptions.
    """
    sections: list[str] = ["## Entity Types\n"]

    for etype in entity_types:
        desc = ENTITY_TYPE_DESCRIPTIONS.get(etype, "")
        sections.append(f"### {etype}")
        if desc:
            sections.append(desc)
        sections.append("")

        types = get_claim_types_for_entity(etype)
        if types:
            sections.append("Claim types:")
            for ct in types:
                sections.append(f"  - {ct.key}: {ct.description}")
        sections.append("")

    return "\n".join(sections)
