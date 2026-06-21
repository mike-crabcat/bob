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
7. Messages from the assistant (the AI agent) often reiterate, recall, or \
summarize information already in memory. Do NOT create bulletins for \
information the assistant is simply repeating or recalling. Only create \
bulletins from assistant messages when the human explicitly confirms, corrects, \
or adds new details to what the assistant said (e.g. "yes, and the flight was \
$450" or "no, it was actually March not April").

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
- **stay-SLUG**: An accommodation leg within a trip — one hotel/Airbnb/villa stay. \
Include location AND date range for uniqueness. \
e.g. stay-ubud-days4-6, stay-paris-june12-14, stay-paris-june14-16. \
Each distinct accommodation (different hotel or different dates at same city) MUST be a separate entity.
- **connection-SLUG**: A transport/journey leg. Include route and direction for uniqueness. \
e.g. connection-perth-geneva-outbound, connection-paris-london-eurostar, connection-chamonix-paris-sncf.
- **location-SLUG**: A place. e.g. location-villa-sunset
- **event-SLUG**: An event. e.g. event-dinner-aug5
- **task-SLUG**: A task or todo. e.g. task-book-villa
- **file-SLUG**: A file or document. e.g. file-itinerary-md. \
  ONLY create if the bulletin contains an actual workspace-relative path or URL. \
  The file_path claim value MUST be a concrete path like "docs/itinerary.md" or \
  "https://example.com/file.pdf". If no path is given, do NOT create a file entity. \
  Vague values like "workspace", "the file", or bare filenames without directories are invalid.
- **thing-SLUG**: A physical object or animal. e.g. thing-ebike, thing-bosch-motor
- **decision-SLUG**: A decision. e.g. decision-stay-seminyak

Slug rules: lowercase, hyphens, descriptive but short. No dates unless needed for uniqueness.

**REUSE EXISTING IDS.** Check the ## Known Entities section. If an entity already exists \
for the thing you are describing, use its ID. Do NOT create duplicate entities with different IDs.
**EXCEPTION for stays:** Two stays with different accommodations, different dates, or different cities \
are DIFFERENT entities — do NOT reuse a stay ID just because both are "in Paris". Each distinct \
hotel/booking gets its own stay entity.

---

# Bulletin Format

Bulletins may be in one of two formats:

1. **Raw transcript** (new). Contains:
   - A header line `Prior messages (context only, do not extract):` followed by N \
     messages. **DO NOT extract claims from this section.** These are repeated for \
     context only; their facts have already been extracted from a previous window.
   - A header line `Window messages:` followed by the window. Extract claims ONLY \
     from messages under this header.
   - Each line has the form `[<iso_ts>] [<name> <contact_id>][SYNTHETIC]: <content>`.
   - Lines tagged `[SYNTHETIC]` are assistant responses generated using memory recall \
     (echoing/summarizing facts already in memory). **DO NOT extract claims from \
     `[SYNTHETIC]` lines.** They are not new ground truth.
2. **Legacy LLM summary** (older bulletins). Plain text with no headers. Extract normally.

---

# Rules

0. Identify the bulletin's format first. If raw transcript: skip the entire \
   "Prior messages (context only, do not extract):" block, and skip any line tagged \
   `[SYNTHETIC]`. Extract only from non-SYNTHETIC lines under "Window messages:".
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
6. Follow the per-entity-type rules listed in the Entity Types section below.

---

# Person Resolution

The bulletin text uses `{{person-slug|Name}}` tags for known people.
- Use the slug from the tag as the entity ID.
- For real people NOT in the tags: `person:new:Full Name`

For raw-transcript bulletins, lines use the bracketed `[Name contact_id]` form \
instead of `{{person-slug|Name}}` tags. If a contact_id appears and matches a \
Known Entity slug, use that slug. Otherwise derive `person:new:Full Name` from \
the displayed Name.

NEVER invent person IDs. Only slugs from tags, existing entities, or `person:new:Full Name`.

---

{claim_types_section}

---

# Self & Relationship Claims — Addressing Guard

The agent this memory belongs to is named **{bot_name}**. In raw transcripts, \
{bot_name}'s own messages have sender tag `[assistant]` (or `[assistant][SYNTHETIC]` \
when echoing recalled memory).

`self-bob` and `relationship-bob-{{person-slug}}` claims describe {bot_name} itself, \
or how a person interacts directly WITH {bot_name}. They must never be written from \
conversations {bot_name} is not part of.

**Before writing any claim whose subject is `self-bob` or `relationship-bob-*`:**

1. **{bot_name} must be a participant.** If there are no `[assistant]` lines under \
"Window messages:", {bot_name} is not in the conversation — skip ALL self and \
relationship claims for this bulletin.
2. **The person must be addressing {bot_name} specifically** — at least one of:
   - Names {bot_name} in their message (e.g. "{bot_name}, what's my schedule?"), or
   - Replies directly to an `[assistant]` line in a 1:1 DM, or
   - Explicitly @-mentions or otherwise directs the message at {bot_name}.
3. **Group chats are presumptively NOT addressed to {bot_name}.** In a group chat \
with multiple humans, a message from person X is usually directed at the group or at \
another human. Only treat it as addressing {bot_name} when (1) or (2) clearly applies.

**Do NOT extract** a `relationship-bob-*` claim when the person is talking to another \
human (e.g. organising squash with a third party, answering another human's question), \
or when {bot_name} is silent in the window.

If in doubt, omit the claim.

---

# Person-Level Claims — Preferences, Interests & Attributes

When a human mentions what they like, dislike, own, play, follow, or do for fun or work, \
extract it as a `person-*` claim. These describe WHO THE PERSON IS and are independent of \
any interaction with {bot_name}. Use subject_id `person-{{slug}}` — NOT `relationship-bob-*`.

How casual utterances map to claim types:

- drink_preference: "I don't really like Pilsner, I'd much prefer a hazy juicy IPA" → \
  value="prefers hazy juicy IPA; dislikes Pilsner"
- food_preference: "I hate coriander" → value="hates coriander"
- dietary_restriction: "I'm coeliac" → value="coeliac"
- sport_preference: "I play squash Tuesdays" → value="plays social squash (Tuesdays)"
- interest: "I drive a Prado 150, love the outdoors" → \
  value="4WDing; outdoors; Toyota Prado 150"
- entertainment_preference: "Big Eagles fan" → value="AFL — Eagles supporter"
- music_preference: "I play guitar, mostly indie rock" → value="plays guitar; indie rock"
- pet: "My golden retriever Bella" → value="golden retriever named Bella"
- job: "I'm a software engineer at Google" → value="Software Engineer, Google"
- workplace: (same utterance) → value="Google"
- hometown: "Grew up in Perth" → value="Perth"
- appearance: "tall bloke, glasses" → value="tall, wears glasses"

**Before finalizing output:** re-scan each non-SYNTHETIC human message and ask: \
"does this reveal a fact about who this person IS — a taste, hobby, possession, trait, \
relationship, or affiliation?" If yes, emit a `person-*` claim. Most casual human \
messages carry at least one such fact, even when the surface topic looks like banter.

Preferences and interests are durable — extracting them is high-value even when the \
surrounding message is joking, sarcastic, or addressed to {bot_name}.

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
  }},
  {{
    "claim_type_key": "drink_preference",
    "subject_id": "person-ryan",
    "value": "prefers hazy juicy IPA or Corona on the water; dislikes Pilsner",
    "status": "active",
    "source_bulletin_id": "bulletin-2026-06-01-001",
    "visibility": "group"
  }},
  {{
    "claim_type_key": "interest",
    "subject_id": "person-david-curnow",
    "value": "4WDing; outdoors; drives Toyota Prado 150; doesn't drink beer",
    "status": "active",
    "source_bulletin_id": "bulletin-2026-06-01-001",
    "visibility": "group"
  }},
  {{
    "claim_type_key": "transport_type",
    "subject_id": "connection-perth-bali-outbound",
    "value": "flight",
    "status": "active",
    "source_bulletin_id": "bulletin-2026-06-01-001",
    "visibility": "group"
  }},
  {{
    "claim_type_key": "departure_location",
    "subject_id": "connection-perth-bali-outbound",
    "value": "Perth PER",
    "status": "active",
    "source_bulletin_id": "bulletin-2026-06-01-001",
    "visibility": "group"
  }},
  {{
    "claim_type_key": "arrival_location",
    "subject_id": "connection-perth-bali-outbound",
    "value": "Bali DPS",
    "status": "active",
    "source_bulletin_id": "bulletin-2026-06-01-001",
    "visibility": "group"
  }},
  {{
    "claim_type_key": "departure_time",
    "subject_id": "connection-perth-bali-outbound",
    "value": "2026-08-01T06:00",
    "status": "active",
    "source_bulletin_id": "bulletin-2026-06-01-001",
    "visibility": "group"
  }},
  {{
    "claim_type_key": "duration",
    "subject_id": "connection-perth-bali-outbound",
    "value": "6h",
    "status": "active",
    "source_bulletin_id": "bulletin-2026-06-01-001",
    "visibility": "group"
  }},
  {{
    "claim_type_key": "route",
    "subject_id": "connection-perth-bali-outbound",
    "value": "QZ541 PER→DPS",
    "status": "active",
    "source_bulletin_id": "bulletin-2026-06-01-001",
    "visibility": "group"
  }},
  {{
    "claim_type_key": "connection",
    "subject_id": "trip-bali-2026",
    "object_id": "connection-perth-bali-outbound",
    "status": "active",
    "source_bulletin_id": "bulletin-2026-06-01-001",
    "visibility": "group"
  }}
]
```

If no claims can be extracted, return `[]`.
Return ONLY the JSON array. No other text."""


def build_extraction_prompt(claim_types_section: str, bot_name: str = "Bob") -> str:
    """Build the claim extraction prompt with injected claim types and bot name."""
    return _CLAIM_EXTRACTION_TEMPLATE.format(
        claim_types_section=claim_types_section,
        bot_name=bot_name,
    )


_SILENT_TURN_TEMPLATE = """\
You are {bot_name}, an AI assistant. A conversation in this channel has just gone \
idle, and you have a quiet moment to reflect on it. Read the recent message history \
below and decide: **is there anything worth remembering long-term** about the people, \
groups, trips, or other entities involved?

This is a *silent* turn — you are NOT replying to anyone. Your only job is to record \
anything memory-worthy using the memory tools, then stop. Do not produce a chat reply.

---

# Whose messages to learn from

**Only form memories from messages authored by OTHER people (role: user).** Never \
extract from your own messages (role: assistant) — they are your own output, not ground \
truth, and treating them as facts would create feedback loops. Keep your own messages in \
mind only for context (to understand what someone is replying to).

Some of your own (assistant) messages are prefixed `[SYNTHETIC]` — those were generated \
using memory recall, i.e. they echo or summarise things already in memory. If a person's \
reply is responding to a `[SYNTHETIC]` message, treat it as **corroboration of existing \
memory, not a fresh assertion**: record it at most once, at lower confidence, and do not \
mint a brand-new entity purely on the strength of confirming something you already said.

---

# How to record (use the tools)

You have four tools: `list_entities`, `get_entity`, `create_entity`, `add_claim`.

1. **Before writing, look.** Call `list_entities` / `get_entity` to check whether the \
person or entity already exists and what is already known about them. This avoids \
duplicate claims — if a fact is already recorded, do not record it again.
2. **New entity?** Use `create_entity` (entity_id, entity_type, optional claims_json) \
for people/trips/groups/etc. that don't yet exist, then `add_claim` for each fact.
3. **Existing entity?** Use `add_claim` (subject_id, claim_type_key, value or object_id) \
to add the new fact.
4. **Nothing worth remembering?** Do nothing and stop. Most idle windows record little \
or nothing — that is the correct outcome. Do not invent facts to justify the turn.

Every claim you write is automatically attributed to this turn, so you do not need to \
track provenance yourself.

---

# Entity IDs

Reference entities by ID using these conventions:
- **person-SLUG**: people (e.g. person-mike-cleaver). For someone not yet in memory, use \
  `person:new:Full Name` as the subject_id and create them first.
- **group-SLUG**: chat groups/teams. **trip-SLUG**: trips. **location-SLUG**: places.
- **stay-SLUG**: one accommodation leg (hotel/villa) within a trip — include location \
  and date range for uniqueness (e.g. stay-ubud-days4-6).
- **connection-SLUG**: a transport/journey leg — include route + direction \
  (e.g. connection-perth-bali-outbound).
- **event-SLUG**: events. **task-SLUG**: tasks. **decision-SLUG**: decisions.
- **file-SLUG**: only when a real workspace-relative path or URL is mentioned; the file \
  path value must be concrete (e.g. docs/itinerary.md). Never invent paths.

Slug rules: lowercase, hyphens, short and descriptive. Reuse existing IDs whenever the \
entity already exists — do not create duplicates.

---

{claim_types_section}

---

# What is worth remembering (and what isn't)

Record DURABLE facts a person genuinely states about themselves or their life: confirmed \
plans/decisions/bookings, trip and travel details, dietary or health restrictions, job \
and workplace, hometown, family and relationships, important dates, and clear personal \
tastes they actually hold (e.g. "I'm vegetarian", "I drive a Prado", "I follow the Eagles").

**Do NOT record:**
- Jokes, hypotheticals, "wouldn't it be funny if…", or group banter/riffs as facts or \
  preferences. If a statement is playful, ironic, a thought experiment, or the group \
  riffing on a silly idea, it is NOT a preference — skip it entirely.
- Something attached to the wrong person. Only record a fact against the person who \
  actually stated it or clearly owns it. Never transfer one person's possession, trait, \
  or taste onto another participant.
- Multiple claims about the same topic for the same person — consolidate into one claim.
- Greetings, acknowledgements, emoji reactions, scheduling chatter, or who-said-what logs.

**Attribution rule (read carefully):** when a group discusses an object or topic — a car, \
a trip, a gadget, a running joke — record it ONLY for the person who owns it or who stated \
it as their own. Do not spread it across participants. If you cannot tell who it belongs \
to, do not record it at all.

Use the most specific preference type available (drink_preference, food_preference, \
sport_preference, interest, etc.) rather than a generic "preference" when one fits.

Use conservative wording: a suggestion is not a decision, a mention is not a task, a \
possible plan is not a confirmed booking. When in doubt, omit.

---

# Common miscategorization traps (read carefully)

The `preference` and `truth` claim types have strict definitions that are easy to \
violate by treating them as catch-alls. Do not do this.

**`preference` is for DURABLE personal tastes ONLY** ("prefers dark mode", "prefers \
red wine", "is an early bird"). It is NOT a junk drawer for anything Mike expresses \
interest in. Specifically, do NOT record these as `preference`:

- Skill feature requests or design asks — "wants the trip-planning skill to convert \
  currencies to AUD", "wants the GIF skill changed back from random mode". These are \
  one-off task asks about a tool, not personal tastes. Either record them as a `task` \
  on the relevant skill, or skip them entirely.
- Action items / one-off requests — "wants someone to tell David X", "wants a torrent \
  link shared with David". These are tasks, not preferences.
- Questions — "wants to know whether steaks should be salted immediately". A question \
  is not a preference. Do not record it.
- Scheduling chatter — "will miss pre-drinks on 2026-06-12", "in for Friday 12 June", \
  "BYOB because he only had 5 beers left". Already excluded above. Skip.
- Past actions — "requested an immediate call", "asked how to wire the image skill". \
  These describe a single event in the past, not a stable taste. Skip.
- Trivia disguised as preference — "favourite number is 42", "favourite animal tiger". \
  These are not durable personal tastes in the intended sense. Skip.

When a `preference` value starts with "wants", "wants the X skill", "wants to know", \
"asked for", "requested", or "wants someone to" — that is almost always a miscategorization. \
Re-read it and either route it to `task` or skip it.

**`truth` is ONLY for explicit user corrections of existing memory** ("actually...", \
"no, it's X", "that's wrong"). It is NOT a fact bucket. Do NOT record these as `truth`:

- Narrative notes about what happened — "the assistant later confirmed the message was \
  sent to David on 2026-05-11", "Claude was not actually invoked in the visible flow". \
  These are observations, not corrections. Skip.
- General facts — "There does not appear to be an official 'Qwen 3.6 120B' model", \
  "the GIPHY API uses search terms". These belong on a relevant entity (file, thing, \
  location) as an appropriate typed claim, or are not memory-worthy at all.
- Past actions — "asked to get Claude to fix the BOM weather skill", "approved the \
  Claude delegation". These are event logs, not corrections. Skip.
- Meta-commentary — "the earlier claim was flippant", "misread the order of the \
  exchange". Skip.

A genuine `truth` correction reads like: "no, the message to Gareth went by WhatsApp, \
not email", or "actually it's 2 stops in Paris, not 1". If the value does not contradict \
a previously-recorded claim, it is not a `truth`.

**`milestone` (on `self-bob`) is for qualitative lifecycle events ONLY** — firsts, \
breakthroughs, regime changes in Bob's capability or role. Examples: "first solo \
multi-step task completed", "first time Mike delegated a booking decision without \
checking". It is NOT a place to record changes to Bob's code or configuration. Do NOT \
record these as `milestone`:

- Changelog entries or release notes — "Added a /approve slash command", "Added a \
  WhatsApp media /upload endpoint". These belong in CHANGELOG.md, not memory.
- Refactor descriptions — "Refactored huge modules into saner packages", "Renamed \
  cost columns from prompt/completion to input/output". Belong in git log.
- Bug-fix summaries — "Fixed file logging going silent after startup", "Fixed a \
  routine scheduler double-fire race". Belong in git log.
- Routine feature work — "Added daily rotating logs", "Added docs/datamodel.md with \
  ERDs". Belong in CHANGELOG.md.

The test: would this milestone change how Bob thinks about itself or how it should \
behave? If it's just "we shipped X" or "we fixed Y", it fails the test — skip it. \
When changelog text reaches you via a bulletin, do not extract it at all.

{group_context}
"""


def build_silent_turn_prompt(
    claim_types_section: str,
    bot_name: str = "Bob",
    group_context: str = "",
) -> str:
    """Build the system prompt for a silent-turn extraction turn.

    `group_context` is an optional pre-rendered block describing the channel's
    group/participant context (injected for group chats and DMs alike).
    """
    return _SILENT_TURN_TEMPLATE.format(
        claim_types_section=claim_types_section,
        bot_name=bot_name,
        group_context=group_context,
    )


RETRIEVAL_AGENT_PROMPT = """\
You are a memory retrieval agent operating against the Agent Memory System.

Your objective is to answer the user's question using the minimum amount of memory required while maintaining provenance and privacy.

The memory system contains:

- Persons (people)
- Groups
- Locations
- Trips
- Stays (accommodation legs within trips)
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
