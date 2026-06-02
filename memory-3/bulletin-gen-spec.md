# Bulletin Generation Process Specification

## Purpose

This document specifies how a session transcript range is converted into a memory bulletin for the Agent Memory System.

It is intended for implementation by a coding agent.

The bulletin generation process sits before the main memory ingestion pipeline.

```text
Session Transcript Range
        ↓
Bulletin Generator
        ↓
Draft Bulletin
        ↓
Validation / Entity Resolution / Claim Extraction
        ↓
Immutable Bulletin Store
        ↓
Derived Memory Updates
```

The generator's job is not to update memory directly.

The generator's job is to produce a high-quality draft bulletin, or decide that no bulletin should be created.

---

# 1. Key Concepts

## Session

A session is a bounded interaction context.

Examples:

- WhatsApp conversation window
- Agent-user chat session
- Workspace activity session
- Email thread processing session
- Calendar review session
- Manual note entry session

## Transcript Range

A transcript range is a subset of a session selected for memory processing.

Example:

```text
session_id: session-whatsapp-bali-2026-001
transcript_range_id: range-0004
start_time: 2026-06-01T09:00:00+08:00
end_time: 2026-06-01T09:30:00+08:00
```

## Bulletin

A bulletin is an immutable source record representing memory-worthy information extracted from a transcript range.

Bulletins are the source of truth for the memory system.

## Draft Bulletin

A draft bulletin is the generator output before the ingestion pipeline validates, enriches, and commits it.

Draft bulletins may use:

```yaml
id: pending
```

The ingestion pipeline assigns the final bulletin ID.

---

# 2. Process Overview

```text
1. Select transcript range
2. Provide metadata and transcript to bulletin generator
3. Generator determines memory worthiness
4. Generator emits either:
   - no-bulletin response
   - draft bulletin markdown
5. Ingestion pipeline validates draft bulletin
6. Entity resolution occurs
7. Claims are extracted or refined
8. Final immutable bulletin is written
9. Derived memory is updated
```

---

# 3. Inputs to Bulletin Generator

The bulletin generator should receive a structured input object.

```yaml
session_id: session-whatsapp-bali-2026-001

transcript_range_id: range-0004

transcript_start_time: 2026-06-01T09:00:00+08:00

transcript_end_time: 2026-06-01T09:30:00+08:00

channel_id: channel-whatsapp-bali-trip

channel_type: whatsapp_group

source_type: session_transcript_range

actor_contact_id: contact-7f3a91

visibility: group

scope:
  - group-bali-travellers

known_entities:
  contacts:
    - id: contact-7f3a91
      display_name: Michael Cleaver
    - id: contact-91bd22
      display_name: Sarah
  groups:
    - id: group-bali-travellers
      display_name: Bali Travellers
  trips:
    - id: trip-bali-2026
      display_name: Bali 2026
  channels:
    - id: channel-whatsapp-bali-trip
      display_name: WhatsApp Bali Trip Chat

transcript: |
  <transcript text>
```

---

# 4. Generator Responsibilities

The bulletin generator is responsible for:

1. Reading the transcript range.
2. Deciding whether memory-worthy information exists.
3. Extracting durable memory updates.
4. Distinguishing facts, decisions, suggestions, tasks, artifacts, preferences, and constraints.
5. Referencing contacts by contact ID where known.
6. Using known entity IDs where supplied.
7. Proposing candidate IDs for non-contact entities where necessary.
8. Marking unresolved contacts for review.
9. Preserving privacy scope from the source context.
10. Producing exactly one markdown bulletin or an explicit no-bulletin response.

The generator must not:

- Edit entity documents.
- Edit claims.
- Write to indexes.
- Invent contact IDs.
- Broaden visibility beyond the channel context.
- Convert casual chatter into durable memory.
- Treat suggestions as decisions.
- Treat mentions as tasks.
- Treat possibilities as bookings.

---

# 5. Memory-Worthiness Rules

The generator should create a bulletin only if the transcript contains durable information likely to matter later.

## Create a bulletin for:

```text
Decision made
Task assigned
Task completed
Task cancelled
Task owner changed
Plan changed
Trip detail added
Trip location added or changed
Travel dates added or changed
Preference expressed
Constraint identified
Booking confirmed
Availability changed
Artifact created
Artifact modified
Important file discussed
Relationship between entities clarified
Private note worth remembering
Important commitment made
```

## Usually do not create a bulletin for:

```text
Greetings
Acknowledgements
Emoji-only reactions
Thanks
OK
Casual jokes
Small talk
Duplicate confirmations
Unsupported guesses
Short-lived comments
General chatter
Messages with no future relevance
```

## Conservative Bias

The generator should be conservative.

It is better to skip weak memory than to pollute the memory system.

When uncertain, the generator may set:

```yaml
requires_review: true
```

---

# 6. Entity Rules

## Contact References

Contacts must be referenced by contact ID.

Correct:

```yaml
contacts:
  - id: contact-7f3a91
```

Incorrect:

```yaml
contacts:
  - id: Michael Cleaver
```

The generator must not invent contact IDs.

If a contact is mentioned but no contact ID is available, create an unresolved contact entry:

```yaml
contacts:
  - id: unresolved-contact-session-whatsapp-bali-2026-001-001
    matched_from: "Tom"
    resolution_status: needs_review
```

## Non-Contact Entity References

For groups, channels, trips, locations, tasks, artifacts, events, and decisions:

- Use known IDs where available.
- Propose stable candidate IDs if required.
- Use kebab case.
- Mark proposed IDs clearly.

Example:

```yaml
trips:
  - id: trip-bali-2026
    label: "Bali 2026"
    resolution_status: known

locations:
  - id: location-seminyak
    label: "Seminyak"
    resolution_status: proposed
```

## Relationship References

All relationships must use IDs, not display names.

Example:

```text
task-compare-villas is owned by contact-7f3a91.
artifact-villa-spreadsheet supports trip-bali-2026.
decision-focus-accommodation-seminyak applies to trip-bali-2026.
```

---

# 7. Distinction Rules

The generator must distinguish between memory types.

## Suggestion vs Decision

A suggestion is not a decision.

Example transcript:

```text
Sarah: Maybe we should stay in Seminyak.
```

Correct memory:

```text
Sarah suggested Seminyak.
```

Incorrect memory:

```text
The group decided to stay in Seminyak.
```

A decision requires explicit agreement, commitment, or clear resolution.

## Mention vs Task

A mention is not a task.

Example transcript:

```text
Michael: We should probably check passports.
```

Possible memory:

```text
Passport checking was raised as a possible task.
```

Only create an active task if there is an owner or clear commitment.

## Possible Plan vs Booking

A possible plan is not a confirmed booking.

Example transcript:

```text
Sarah: Flights on July 7 look good.
```

Correct:

```text
July 7 flights were discussed.
```

Incorrect:

```text
Flights are booked for July 7.
```

## Preference vs Constraint

A preference is desirable.

A constraint restricts options.

Example:

```text
Preference: direct flights preferred.
Constraint: cannot travel before July 5.
```

---

# 8. Privacy Rules

The bulletin generator must preserve privacy from the source context.

It must not broaden visibility.

If the input says:

```yaml
visibility: group
scope:
  - group-bali-travellers
```

the output must not become:

```yaml
visibility: public
```

If sensitive personal information appears, use stricter handling:

```yaml
visibility: private
sensitivity: personal
requires_review: true
review_reasons:
  - sensitive_personal_information
```

Sensitive information includes:

- health details
- family conflict
- financial stress
- private worries
- legal issues
- intimate or personal relationship details
- anything that should not be repeated into a group context

---

# 9. Output Contract

The generator must output exactly one of two things:

1. A no-bulletin response.
2. A single draft bulletin in markdown.

No explanatory prose should appear outside the output.

---

## 9.1 No-Bulletin Response

If no durable memory-worthy information exists:

```markdown
---
create_bulletin: false
reason: "No durable memory-worthy information found."
session_id: "session-whatsapp-bali-2026-001"
transcript_range_id: "range-0004"
---
```

---

## 9.2 Draft Bulletin Output

If memory-worthy information exists, output:

```markdown
---
create_bulletin: true

id: pending

session_id: "<SESSION_ID>"

transcript_range_id: "<TRANSCRIPT_RANGE_ID>"

created_at: "<TRANSCRIPT_END_TIME>"

channel_id: "<CHANNEL_ID>"

source_type: "session_transcript_range"

visibility: "<VISIBILITY>"

scope:
  - "<SCOPE_ID>"

entities:
  contacts: []
  groups: []
  channels:
    - id: "<CHANNEL_ID>"
  trips: []
  locations: []
  events: []
  tasks: []
  artifacts: []
  decisions: []

memory_types: []

confidence: high | medium | low

requires_review: true | false

review_reasons: []
---

# Update

Short factual summary of what changed or was learned.

# Extracted Memory

## Decisions

- Use only if a decision was actually made.

## Tasks

- Include owner ID if known.
- Include due date if stated.
- Include status if known.

## Preferences

- Include durable preferences only.

## Constraints

- Include constraints relevant to future planning.

## Artifacts

- Include files created, modified, or discussed.
- Include workspace path if known.
- Include why the artifact exists.

## Trip Details

- Include trip dates, locations, travellers, bookings, or planning changes.

## Other Facts

- Include other durable facts worth remembering.

# Proposed Claims

- Write atomic claim-like statements.
- Use IDs where possible.
- Keep each claim independently understandable.

# Source Notes

Briefly explain which transcript details support this bulletin.

Do not quote large parts of the transcript.
```

---

# 10. Prompt Template

Use the following prompt when invoking an LLM to generate a bulletin.

```text
You are a Bulletin Generator for the Agent Memory System.

Your job is to read a bounded session transcript range and produce a single memory bulletin.

A bulletin is an immutable source record created from a session range. It should capture only information that is worth remembering.

Do not update entity documents.
Do not update claims files.
Do not invent contact IDs.
Do not invent facts not supported by the transcript.
Do not include conversational noise.
Do not broaden privacy scope.

The bulletin you produce will later be processed by the memory ingestion pipeline.

---

# Inputs

You will receive:

1. session_id
2. channel_id
3. transcript_range_id
4. transcript_start_time
5. transcript_end_time
6. actor_contact_id, if known
7. channel visibility and scope
8. known entity hints, if available
9. transcript text

---

# Memory-Worthiness Rules

Create a bulletin only if the transcript contains one or more of:

- decision made
- task assigned
- task completed
- plan changed
- preference expressed
- constraint identified
- booking confirmed
- artifact created or modified
- important location mentioned
- trip detail added
- availability changed
- relationship between entities clarified
- private note or sensitive preference worth remembering

Do not create memory from:

- greetings
- acknowledgements
- jokes with no lasting relevance
- emoji-only reactions
- casual chatter
- duplicate confirmations
- unsupported guesses
- temporary wording that does not affect future state

If there is nothing worth remembering, output exactly:

---
create_bulletin: false
reason: "No durable memory-worthy information found."
session_id: "<SESSION_ID>"
transcript_range_id: "<TRANSCRIPT_RANGE_ID>"
---

---

# Contact and Entity Rules

Contacts must be referenced by contact IDs only.

If a contact ID is known, use it.

If a person/contact is mentioned but no ID is available, include an unresolved contact reference.

Example:

contacts:
  - id: unresolved-contact-session-123-001
    matched_from: "Sarah"
    resolution_status: needs_review

Do not guess contact IDs.

For non-contact entities, use known IDs if supplied.

If no known ID exists, propose a stable candidate ID using kebab case.

Examples:

trips:
  - id: trip-bali-2026
    label: "Bali 2026"
    resolution_status: proposed

locations:
  - id: location-seminyak
    label: "Seminyak"
    resolution_status: proposed

All relationship references must use IDs, not names.

---

# Distinction Rules

A suggestion is not a decision.

A mention is not a task.

A possible plan is not a confirmed booking.

A preference is not a constraint.

Use conservative wording when the transcript is ambiguous.

---

# Privacy Rules

Set visibility and scope from the session/channel context unless the transcript clearly requires stricter handling.

If sensitive personal information appears, mark:

visibility: private
sensitivity: personal
requires_review: true

Never broaden visibility beyond the source context.

---

# Bulletin Output Format

If memory-worthy information exists, output exactly one markdown bulletin using this format:

---
create_bulletin: true

id: pending

session_id: "<SESSION_ID>"

transcript_range_id: "<TRANSCRIPT_RANGE_ID>"

created_at: "<TRANSCRIPT_END_TIME>"

channel_id: "<CHANNEL_ID>"

source_type: "session_transcript_range"

visibility: "<VISIBILITY>"

scope:
  - "<SCOPE_ID>"

entities:
  contacts: []
  groups: []
  channels:
    - id: "<CHANNEL_ID>"
  trips: []
  locations: []
  events: []
  tasks: []
  artifacts: []
  decisions: []

memory_types: []

confidence: high | medium | low

requires_review: true | false

review_reasons: []
---

# Update

Write a short factual summary of what changed or was learned.

# Extracted Memory

## Decisions

- Use only if a decision was actually made.

## Tasks

- Include owner ID if known.
- Include due date if stated.
- Include status if known.

## Preferences

- Include durable preferences only.

## Constraints

- Include constraints relevant to future planning.

## Artifacts

- Include files created, modified, or discussed.
- Include workspace path if known.
- Include why the artifact exists.

## Trip Details

- Include trip dates, locations, travellers, bookings, or planning changes.

## Other Facts

- Include other durable facts worth remembering.

# Proposed Claims

- Write atomic claim-like statements.
- Use IDs where possible.
- Keep each claim independently understandable.

# Source Notes

Briefly explain which transcript details support this bulletin.

Do not quote large parts of the transcript.

---

# Transcript

The transcript range follows.

<TRANSCRIPT_TEXT>
```

---

# 11. Example Input

```yaml
session_id: session-whatsapp-bali-2026-001

transcript_range_id: range-0004

transcript_start_time: 2026-06-01T09:00:00+08:00

transcript_end_time: 2026-06-01T09:30:00+08:00

channel_id: channel-whatsapp-bali-trip

channel_type: whatsapp_group

source_type: session_transcript_range

actor_contact_id: contact-7f3a91

visibility: group

scope:
  - group-bali-travellers

known_entities:
  contacts:
    - id: contact-7f3a91
      display_name: Michael Cleaver
    - id: contact-91bd22
      display_name: Sarah
  groups:
    - id: group-bali-travellers
      display_name: Bali Travellers
  trips:
    - id: trip-bali-2026
      display_name: Bali 2026
  channels:
    - id: channel-whatsapp-bali-trip
      display_name: WhatsApp Bali Trip Chat

transcript: |
  Sarah: I think Seminyak makes more sense than Ubud for the kids.
  Michael: Agree. Let's focus accommodation research on Seminyak.
  Michael: I created a villa comparison spreadsheet in the Bali folder.
  Sarah: Great. Can you compare the villa options?
  Michael: Yes, I'll do that.
```

---

# 12. Example Output

```markdown
---
create_bulletin: true

id: pending

session_id: "session-whatsapp-bali-2026-001"

transcript_range_id: "range-0004"

created_at: "2026-06-01T09:30:00+08:00"

channel_id: "channel-whatsapp-bali-trip"

source_type: "session_transcript_range"

visibility: "group"

scope:
  - "group-bali-travellers"

entities:
  contacts:
    - id: "contact-7f3a91"
      role: "task_owner"

    - id: "contact-91bd22"
      role: "participant"

  groups:
    - id: "group-bali-travellers"

  channels:
    - id: "channel-whatsapp-bali-trip"

  trips:
    - id: "trip-bali-2026"

  locations:
    - id: "location-seminyak"
      label: "Seminyak"
      resolution_status: "proposed"

  events: []

  tasks:
    - id: "task-compare-villas"
      label: "Compare Villas"
      resolution_status: "proposed"

  artifacts:
    - id: "artifact-villa-spreadsheet"
      label: "Bali Villa Comparison Spreadsheet"
      resolution_status: "proposed"

  decisions:
    - id: "decision-focus-accommodation-seminyak"
      label: "Focus Accommodation Research On Seminyak"
      resolution_status: "proposed"

memory_types:
  - decision
  - task
  - artifact
  - trip_detail

confidence: medium

requires_review: false

review_reasons: []
---

# Update

The Bali trip group agreed to focus accommodation research on Seminyak. A villa comparison spreadsheet was created for the trip, and contact-7f3a91 took responsibility for comparing villa options.

# Extracted Memory

## Decisions

- decision-focus-accommodation-seminyak: Accommodation research for trip-bali-2026 should focus on location-seminyak.

## Tasks

- task-compare-villas is open.
- task-compare-villas is owned by contact-7f3a91.
- task-compare-villas relates to trip-bali-2026 and location-seminyak.

## Preferences

- The group prefers location-seminyak over Ubud for kid-friendly accommodation.

## Constraints

- Kid-friendly accommodation is important for trip-bali-2026.

## Artifacts

- artifact-villa-spreadsheet was created for trip-bali-2026.
- The artifact is intended to compare villa options.

## Trip Details

- trip-bali-2026 is actively evaluating accommodation in location-seminyak.

## Other Facts

- channel-whatsapp-bali-trip is being used to coordinate trip-bali-2026.

# Proposed Claims

- trip-bali-2026 has active accommodation focus location-seminyak.
- decision-focus-accommodation-seminyak applies to trip-bali-2026.
- task-compare-villas is open.
- task-compare-villas is owned by contact-7f3a91.
- artifact-villa-spreadsheet was created for trip-bali-2026.
- artifact-villa-spreadsheet supports task-compare-villas.
- trip-bali-2026 has accommodation constraint kid-friendly.

# Source Notes

The transcript states that Sarah preferred Seminyak over Ubud for the kids, Michael agreed to focus accommodation research on Seminyak, Michael created a villa comparison spreadsheet, and Michael agreed to compare villa options.
```

---

# 13. Validation Requirements

After generation, the ingestion pipeline should validate:

## Required Metadata

- `create_bulletin`
- `session_id`
- `transcript_range_id`
- `created_at`
- `channel_id`
- `source_type`
- `visibility`
- `scope`
- `entities`
- `confidence`
- `requires_review`

## Duplicate Protection

The tuple below should be unique:

```text
channel_id + session_id + transcript_range_id
```

or, when available:

```text
source_type + source_id
```

## Entity Validation

- contact IDs must exist in Contacts Database, unless marked unresolved.
- channel ID must exist.
- known group/trip/location/task/artifact/decision IDs should resolve to existing entities or be marked proposed.
- proposed IDs must be stable and kebab-case.

## Privacy Validation

- visibility must not be broader than source channel default.
- sensitive items require review.
- private notes must not be scoped to a group unless explicitly intended.

---

# 14. Error Handling

## No Memory Found

Use no-bulletin response.

## Ambiguous Contact

Create unresolved contact entry and set:

```yaml
requires_review: true
review_reasons:
  - unresolved_contact
```

## Ambiguous Decision

Record as suggestion or preference, not decision.

Optionally set:

```yaml
requires_review: true
review_reasons:
  - ambiguous_decision
```

## Conflicting Information

Do not resolve conflict in the bulletin.

Record the new information and allow claim/entity processing to handle lifecycle.

Set:

```yaml
requires_review: true
review_reasons:
  - possible_conflict
```

## Sensitive Information

Set stricter privacy.

```yaml
visibility: private
sensitivity: personal
requires_review: true
review_reasons:
  - sensitive_information
```

---

# 15. Implementation Notes

## Recommended Generator Function

```text
generate_bulletin_from_transcript_range(input) -> markdown
```

## Recommended Pipeline

```text
select transcript range
    ↓
build generator input
    ↓
invoke LLM with bulletin generation prompt
    ↓
parse markdown frontmatter
    ↓
validate output contract
    ↓
write to staging
    ↓
ingestion pipeline processes staged bulletin
```

## Recommended Tests

Create fixtures for:

1. No memory-worthy content.
2. Clear decision.
3. Suggestion that is not a decision.
4. Task with owner.
5. Possible task without owner.
6. Artifact creation.
7. Booking confirmed.
8. Sensitive private note.
9. Ambiguous contact.
10. Conflicting plan update.
11. Duplicate transcript range.
12. Group visibility preservation.

---

# 16. Golden Test Cases

## No Bulletin

Transcript:

```text
Michael: Thanks
Sarah: 👍
Michael: OK
```

Expected:

```yaml
create_bulletin: false
```

## Suggestion Only

Transcript:

```text
Sarah: Maybe we should look at Ubud too.
```

Expected:

```text
Suggestion or preference, not decision.
```

## Clear Decision

Transcript:

```text
Sarah: Seminyak seems better for the kids.
Michael: Agreed. Let's focus on Seminyak.
```

Expected:

```text
Decision or strong preference to focus accommodation research on Seminyak.
```

## Task Assignment

Transcript:

```text
Sarah: Can you compare the villas?
Michael: Yes, I'll do that.
```

Expected:

```text
Open task owned by Michael's contact ID.
```

## Artifact Creation

Transcript:

```text
Michael: I created the villa comparison spreadsheet in the Bali folder.
```

Expected:

```text
Artifact entity proposed or referenced.
Artifact linked to trip if trip context is known.
```

---

# 17. Final Summary

The bulletin generator converts bounded transcript ranges into durable memory source records.

It should be conservative, privacy-aware, ID-oriented, and provenance-preserving.

Its output is not final memory.

Its output is a draft bulletin for the ingestion pipeline.

The best generator behavior is:

```text
Remember durable changes.
Ignore conversational noise.
Use IDs.
Do not guess contact identity.
Do not overstate certainty.
Do not broaden privacy.
Produce one clean markdown bulletin or no bulletin.
```