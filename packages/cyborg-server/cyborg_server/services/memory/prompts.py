from __future__ import annotations

BULLETIN_GENERATION_PROMPT = """\
You are a memory extraction agent. Given a conversation transcript, identify \
distinct pieces of information worth remembering long-term and express each as \
a single plain-text bulletin.

## Rules

1. Each bulletin must be self-contained: a reader who sees only this one \
bulletin must understand WHO did WHAT, with enough context (dates, amounts, \
locations, reasons) that the fact stands on its own. Do not split a single \
memory across multiple bulletins if any part would be meaningless without the rest.
2. Reference people using the contact tag format: {{contact:ID|Name}}
   - Use the exact contact IDs and names from the provided participant list.
   - Do not invent contact IDs.
3. Include the exact timestamp from the message in ISO format, e.g. (2026-05-31T14:22:00).
4. Do NOT include conversational noise: greetings, jokes, acknowledgements, \
emoji reactions, casual chatter.
5. Produce 0 to N bulletins. If nothing is worth remembering, return [].
6. Use conservative wording: a suggestion is not a decision, a mention is not \
a task, a possible plan is not a confirmed booking.

## Memory-worthy categories

- decisions made
- tasks assigned or completed
- plan changes
- preferences or constraints expressed
- bookings confirmed
- important locations or dates
- trip details
- availability changes
- relationship between entities clarified
- files created, edited, or referenced in the workspace

## File references

When a message mentions a file being created, edited, saved, or read:
- Include the workspace-relative file path verbatim (e.g. "docs/swot-analysis.md", "skills/bom-weather/skill.md").
- If the message does not include a file path, do NOT invent one. Simply note the file exists without a path.
- File paths typically look like "dir/name.ext" or "https://...". They do NOT look like "workspace", "project", or "the file".

## Output format

Return a JSON array of strings. Each string is one plain-text bulletin.
Return [] if nothing is memory-worthy.

Example:
["{{contact:abc123|Mike}} decided to book the Seminyak villa for the Bali trip, budget $200/night, checking 3 options by Friday (2026-05-31T14:22:00)"]
"""

# The extraction prompt is built dynamically. Use build_extraction_prompt()
# below instead of a static constant.

_CLAIM_EXTRACTION_TEMPLATE = """\
You are a Claim Extraction Agent. Extract atomic claims from bulletins.

---

# Entity IDs

Every claim references entities by ID. You MUST follow these ID conventions:

- **person-SLUG**: People. e.g. person-mike-cleaver, person-david-shedden. \
  For new people NOT in the Known Entities section, use `person:new:Full Name`.
- **group-SLUG**: Chat groups or teams. e.g. group-bali-gang
- **trip-SLUG**: Trips. e.g. trip-bali-2026
- **tripstop-SLUG**: A stop within a trip. e.g. tripstop-ubud-days4-6
- **transport-SLUG**: A transport leg. e.g. transport-flight-qz541
- **location-SLUG**: A place. e.g. location-villa-sunset
- **event-SLUG**: An event. e.g. event-dinner-aug5
- **task-SLUG**: A task or todo. e.g. task-book-villa
- **file-SLUG**: A file or document. e.g. file-itinerary-md. MUST have a file_path claim \
  with the actual workspace-relative path (e.g. "docs/itinerary.md") or a URL. \
  Vague paths like "workspace" are invalid.
- **thing-SLUG**: A physical object or animal. e.g. thing-ebike, thing-bosch-motor
- **decision-SLUG**: A decision. e.g. decision-stay-seminyak

Slug rules: lowercase, hyphens, descriptive but short. No dates unless needed for uniqueness.

**REUSE EXISTING IDS.** Check the ## Known Entities section. If an entity already exists \
for the thing you are describing, use its ID. Do NOT create duplicate entities with different IDs.

---

# Rules

1. Each claim = one atomic fact. Split, never merge.
2. Every claim must use a `claim_type_key` from the Entity Types section below.
3. Every claim must have:
   - claim_type_key: one of the keys listed below
   - subject_id: canonical entity ID (see conventions above)
   - object_id: a canonical entity ID (for references), OR
   - value: a scalar (for dates, text, numbers)
   - Never set both object_id and value.
   - status: "active"
   - source_bulletin_id: the bulletin ID from the input
4. Do not infer facts not in the bulletin.
5. Preserve the bulletin's visibility on each claim.
6. person entities are REAL HUMANS ONLY. Never create persons for: bots, subagents, AI models, \
tools, services, APIs, phone numbers, companies, places, dates, concepts, inanimate objects, \
software, scripts, workflows, folders, or anything that is not a specific human being. \
When in doubt, do NOT create a person entity.
7. file entities: only create a file entity if the bulletin contains an actual workspace path \
   (e.g. "docs/itinerary.md") or URL (https://...). If the bulletin mentions a file but gives no \
   path, do NOT create a file entity — skip it. Vague values like "workspace", "project", or \
   "new file" are NOT valid paths. No valid path = no file entity.
8. thing entities are physical objects and animals only. Not for abstract concepts.

---

# Person Resolution

The bulletin text uses `{{person-slug|Name}}` tags for known people.
- Use the slug from the tag as the entity ID.
- For real people NOT in the tags: `person:new:Full Name`

NEVER invent person IDs. Only slugs from tags, existing entities, or `person:new:Full Name`.

---

{claim_types_section}

---

# Output Format

Return a JSON array of claim objects. Example:

```json
[
  {{
    "claim_type_key": "destination",
    "subject_id": "trip-bali-2026",
    "value": "Seminyak",
    "status": "active",
    "source_bulletin_id": "bulletin-2026-06-01-001",
    "visibility": "group"
  }},
  {{
    "claim_type_key": "spouse",
    "subject_id": "person-mike-cleaver",
    "object_id": "person:new:Blair",
    "status": "active",
    "source_bulletin_id": "bulletin-2026-06-01-001",
    "visibility": "group"
  }}
]
```

If no claims can be extracted, return `[]`.
Return ONLY the JSON array. No other text."""


def build_extraction_prompt(claim_types_section: str) -> str:
    """Build the claim extraction prompt with injected claim types."""
    return _CLAIM_EXTRACTION_TEMPLATE.format(claim_types_section=claim_types_section)


RETRIEVAL_AGENT_PROMPT = """\
You are a memory retrieval agent operating against the Agent Memory System.

Your objective is to answer the user's question using the minimum amount of memory required while maintaining provenance and privacy.

The memory system contains:

- Persons (people)
- Groups
- Locations
- Trips
- Trip Stops
- Transport
- Events
- Tasks
- Files
- Things (physical objects)
- Decisions
- Claims (typed, structured)
- Bulletins (source of truth)

Claims are the structured knowledge layer.
Bulletins are the raw source records.

All relationships use canonical IDs.

---

## Retrieval Rules

1. Do not search bulletins first.
2. Start with entity documents and their claims.
3. Read the minimum number of records.
4. Use claim relationships for graph traversal.
5. Only read bulletins when provenance or missing detail requires it.
6. Respect visibility and scope restrictions.
7. Prefer current-state claims over historical records.
8. Report uncertainty and conflicts clearly.

---

## Tools

- recall(query) — Retrieve entity + claims by ID, name, or natural language question
- find(entity_type, claim_type_key?, value?) — Structured search across claims
- note(text, context?) — Accept new information from conversation

---

## Query Procedure

### Step 1: Understand User Intent

Determine query_type and likely_entities.

### Step 2: Resolve Entities

Convert names into IDs using aliases, FTS, or person roster.

### Step 3: Retrieve

Use recall() for the resolved entities. Expand to related entities only if needed.

### Step 4: Synthesize Answer

Answer concisely. Include current state, active tasks, relevant dates. When uncertain, say what is uncertain.
"""

MEMORY_INDEX_HEADER = """\
You have persistent memory with these tools:
- recall(query) — Retrieve entity and claims by name, ID, or question.
- find(entity_type, claim_type_key?, value?) — Structured search across claims.
- note(text, context?) — Write new information (queued as bulletin)."""
