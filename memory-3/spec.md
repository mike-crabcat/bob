# Agent Memory System Design v6

## Purpose

Design a personal agent memory system that:

- Uses markdown as the primary storage format.
- Uses immutable bulletins as the source of truth.
- Supports personal life management.
- Supports WhatsApp groups and conversations.
- Supports trip planning and coordination.
- Tracks workspace artifacts and generated files.
- Supports provenance and explainability.
- Supports privacy-aware retrieval.
- Uses Contacts Database IDs for contacts.
- Uses channels as the source of incoming memory.
- Can be rebuilt entirely from bulletins.
- Uses ripgrep, canonical IDs, graph traversal, and LLM reasoning as primary retrieval mechanisms.
- Does not require embeddings.

---

# 1. Core Architecture

The system observes information through channels.

Channels generate bulletins.

Bulletins generate claims.

Claims update entity documents.

```text
Channel
    ↓
Bulletin
    ↓
Claims
    ↓
Entity Documents
    ↓
Memory Query
```

---

# 2. Design Principles

1. Bulletins are immutable.
2. Channels are the source of bulletins.
3. Contacts come from the Contacts Database.
4. The memory system owns knowledge, not identity.
5. All relationships use canonical IDs.
6. Claims are atomic.
7. Claims have lifecycle states.
8. Entity documents are derived.
9. Every entity document includes Related Entities.
10. Privacy filtering happens before document bodies are read.
11. Trips are first-class entities.
12. Groups are first-class entities.
13. Channels are first-class entities.
14. Artifacts are first-class entities.
15. Current state is prioritized over full history.
16. Everything except bulletins is rebuildable.
17. Simplicity is preferred over sophistication.
18. Embeddings are optional future enhancements, not foundational.

---

# 3. Identity Model

The memory system owns knowledge.

The memory system does not own identity.

## Authoritative Sources

```text
Contacts → Contacts Database
Files    → Workspace / File System
Messages → Channels
```

Contacts must always be referenced by Contacts Database IDs.

Correct:

```yaml
contacts:
  - contact-7f3a91
```

Incorrect:

```yaml
contacts:
  - Michael Cleaver
```

Names are presentation metadata only.

---

# 4. Core Entity Types

```text
Channel
Contact
Group
Location
Trip
Event
Task
Artifact
Decision
```

---

# 5. Directory Structure

```text
memory/

├── bulletins/
│
├── claims/
│
├── entities/
│   ├── channels/
│   ├── contacts/
│   ├── groups/
│   ├── locations/
│   ├── trips/
│   ├── events/
│   ├── tasks/
│   ├── artifacts/
│   └── decisions/
│
├── aliases/
│
├── indexes/
│
├── summaries/
│
└── policies/
```

Only `bulletins/` is authoritative.

Everything else can be regenerated.

---

# 6. Channel Model

Channels represent information sources and communication contexts.

Examples:

```text
channel-whatsapp-family
channel-whatsapp-bali-trip
channel-email-school
channel-calendar-personal
channel-workspace-travel
channel-manual-notes
```

A channel is where information is observed.

A channel may be:

- WhatsApp group
- WhatsApp direct message
- Email thread
- Calendar feed
- Workspace folder
- Manual note stream
- Task manager integration
- Future input source

Channels generate bulletins.

---

# 7. Group Model

Groups represent social relationships.

Examples:

```text
group-family
group-bali-travellers
group-school-parents
```

A group may have many channels.

Example:

```text
group-bali-travellers
    ├── channel-whatsapp-bali-trip
    ├── channel-email-bali-flights
    └── channel-workspace-bali
```

This distinction matters:

```text
Contact = who
Channel = where information came from
Group   = social context
Trip    = planning object
```

---

# 8. Trip Model

Trips are primary planning entities.

Trips aggregate:

- contacts
- groups
- channels
- locations
- tasks
- decisions
- artifacts
- events

Example:

```text
trip-bali-2026
    ├── contact-7f3a91
    ├── group-bali-travellers
    ├── channel-whatsapp-bali-trip
    ├── location-seminyak
    ├── task-book-flights
    ├── decision-stay-seminyak
    └── artifact-villa-spreadsheet
```

Most travel-related queries should start from the trip entity.

---

# 9. Bulletin Ingestion Architecture

All bulletins originate from channels.

```text
Channel
    ↓
Bulletin Adapter
    ↓
Draft Bulletin
    ↓
Validation
    ↓
Entity Resolution
    ↓
Claim Extraction
    ↓
Immutable Bulletin Store
    ↓
Entity Updates
```

---

## 9.1 Bulletin Sources

### WhatsApp

```text
WhatsApp Message
        ↓
WhatsApp Channel
        ↓
Bulletin
```

### Workspace

```text
File Created
        ↓
Workspace Channel
        ↓
Artifact Bulletin
```

### Email

```text
Email Message
        ↓
Email Channel
        ↓
Bulletin
```

### Manual Notes

```text
User Note
        ↓
Manual Notes Channel
        ↓
Bulletin
```

### Calendar

```text
Calendar Event
        ↓
Calendar Channel
        ↓
Bulletin
```

---

## 9.2 Memory Worthiness

Not every observed event becomes a bulletin.

Adapters determine whether information is memory-worthy.

Examples that should create bulletins:

```text
Decision made
Task assigned
Trip updated
Artifact created
Preference expressed
Constraint identified
Booking confirmed
Availability changed
Important file created
Plan changed
```

Examples that should usually not create bulletins:

```text
OK
👍
Thanks
See you tomorrow
General chatter
Duplicate confirmation
```

---

## 9.3 Draft Bulletin

Draft bulletins may be written to a staging area before processing.

```text
memory/staging/bulletins/
```

Example draft:

```markdown
---
id: pending
created_at: 2026-06-01T09:30:00+08:00
channel_id: channel-whatsapp-bali-trip
source_type: whatsapp_message
source_id: whatsapp-message-abc123
visibility: group
scope:
  - group-bali-travellers
---

# Raw Update

Sarah suggested staying in Seminyak and Michael created a villa spreadsheet.
```

---

## 9.4 Validation

The processor validates:

- required frontmatter exists
- source ID has not already been processed
- channel ID exists
- visibility and scope are present
- timestamp is valid
- content is non-empty
- entity references are valid or resolvable

---

## 9.5 Entity Resolution

The processor resolves references using:

- Contacts Database
- aliases
- existing entity documents
- indexes
- LLM extraction where appropriate

Contacts must resolve to Contacts Database IDs.

Unresolved contacts should be placed into a review queue.

Example unresolved contact:

```yaml
contacts:
  - id: unresolved-contact-2026-06-01-001
    matched_from: Tom
    resolution_status: needs_review
```

---

## 9.6 Final Bulletin

Once validated and enriched, the bulletin is written to the immutable bulletin store.

```text
memory/bulletins/2026/06/bulletin-2026-06-01-001.md
```

Example:

```markdown
---
id: bulletin-2026-06-01-001
created_at: 2026-06-01T09:30:00+08:00

channel_id: channel-whatsapp-bali-trip
source_type: whatsapp_message
source_id: whatsapp-message-abc123

visibility: group
scope:
  - group-bali-travellers

entities:
  contacts:
    - contact-7f3a91
    - contact-91bd22

  groups:
    - group-bali-travellers

  channels:
    - channel-whatsapp-bali-trip

  trips:
    - trip-bali-2026

  locations:
    - location-seminyak

  tasks:
    - task-compare-villas

  artifacts:
    - artifact-villa-spreadsheet

  decisions:
    - decision-stay-seminyak
---

# Update

The Bali group prefers Seminyak.

A villa comparison spreadsheet was created.

The accommodation comparison task is now active.
```

---

# 10. Processing Strategy

The system should use eager incremental processing.

When a new bulletin is committed:

```text
New Bulletin
    ↓
Validate
    ↓
Resolve Contact IDs
    ↓
Extract Claims
    ↓
Update Affected Entity Documents
    ↓
Update Aliases
    ↓
Update Indexes
    ↓
Update Summaries If Needed
```

Do not wait until query time to update entity documents.

Reads should be fast and should not serve stale state.

The system should also support full rebuilds.

```bash
memory rebuild --all
memory rebuild --entity trip-bali-2026
memory rebuild --recent
```

---

# 11. Claims

Claims are atomic memory records derived from bulletins.

Claims are stored as markdown documents.

Claims are not the same as bulletins:

- Bulletins are source events.
- Claims are extracted atomic statements.
- Entity documents summarize active claims.

---

## 11.1 Claim Types

```text
fact
preference
constraint
decision
task
availability
booking
artifact
relationship
private_note
```

---

## 11.2 Claim Lifecycle

Claims are never deleted.

Valid states:

```text
active
superseded
retracted
expired
disputed
archived
```

If a decision changes, the old claim is superseded, not deleted.

Example:

```markdown
---
id: claim-2026-06-01-001
type: decision
subject_id: trip-bali-2026
predicate: preferred_location
object_id: location-seminyak
status: superseded
superseded_by:
  - claim-2026-06-10-004
source_bulletins:
  - bulletin-2026-06-01-001
visibility: group
scope:
  - group-bali-travellers
---

# Claim

Bali 2026 prefers Seminyak.
```

---

## 11.3 Claim Example

```markdown
---
id: claim-2026-06-01-002
type: artifact
subject_id: artifact-villa-spreadsheet
predicate: created_for
object_id: trip-bali-2026
status: active
source_bulletins:
  - bulletin-2026-06-01-001
visibility: group
scope:
  - group-bali-travellers
---

# Claim

The Bali Villa Comparison Spreadsheet was created for Bali 2026.
```

---

# 12. Entity Documents

Entity documents are derived summaries.

They are optimized for:

- retrieval
- current-state understanding
- graph traversal
- provenance
- human readability
- machine readability

Every entity document must contain:

- frontmatter with canonical ID
- summary
- current state
- related entities
- timeline or recent activity
- source bulletins

---

# 13. Related Entities

Every entity document must contain a `Related Entities` section.

This is mandatory.

The retrieval system uses Related Entities for graph traversal.

All references must use canonical IDs.

Template:

```yaml
related_entities:
  contacts: []
  groups: []
  channels: []
  trips: []
  locations: []
  events: []
  tasks: []
  artifacts: []
  decisions: []
```

A human can read the entity document to understand current state.

A retrieval agent can navigate the memory graph using Related Entities without needing repeated broad searches.

---

# 14. Entity Specifications

## 14.1 Standard Entity Template

```markdown
---
entity_id:
entity_type:
display_name:
status:
---

# Title

## Summary

## Current State

## Related Entities

## Timeline

## Source Bulletins
```

---

## 14.2 Channel Entity

```markdown
---
entity_id: channel-whatsapp-bali-trip
entity_type: channel
channel_type: whatsapp_group
display_name: WhatsApp Bali Trip Chat
visibility: group
scope:
  - group-bali-travellers
---

# WhatsApp Bali Trip Chat

## Purpose

Coordinate Bali trip planning.

## Current State

Active planning channel for Bali 2026.

## Related Entities

contacts:
  - contact-7f3a91
  - contact-91bd22

groups:
  - group-bali-travellers

channels: []

trips:
  - trip-bali-2026

locations:
  - location-seminyak

events: []

tasks:
  - task-compare-villas

artifacts:
  - artifact-villa-spreadsheet

decisions:
  - decision-stay-seminyak

## Recent Bulletins

- bulletin-2026-06-01-001

## Source Bulletins

- bulletin-2026-06-01-001
```

---

## 14.3 Contact Entity

Contact entities reference the Contacts Database.

The memory system does not create contact identity.

```markdown
---
entity_id: contact-7f3a91
entity_type: contact
contact_source: contacts_db
contact_id: 7f3a91
display_name: Michael Cleaver
---

# Michael Cleaver

## Summary

Contact involved in Bali 2026 planning.

## Current State

Has active planning tasks.

## Preferences

- Prefers practical, implementation-ready design documents.

## Constraints

None known.

## Active Tasks

- task-compare-villas

## Related Entities

contacts: []

groups:
  - group-bali-travellers

channels:
  - channel-whatsapp-bali-trip

trips:
  - trip-bali-2026

locations: []

events: []

tasks:
  - task-compare-villas

artifacts:
  - artifact-villa-spreadsheet

decisions: []

## Source Bulletins

- bulletin-2026-06-01-001
```

---

## 14.4 Group Entity

Groups represent social relationships.

```markdown
---
entity_id: group-bali-travellers
entity_type: group
display_name: Bali Travellers
status: active
---

# Bali Travellers

## Purpose

Coordinate Bali 2026 travel.

## Members

- contact-7f3a91
- contact-91bd22

## Current State

Trip planning in progress.

## Related Entities

contacts:
  - contact-7f3a91
  - contact-91bd22

groups: []

channels:
  - channel-whatsapp-bali-trip

trips:
  - trip-bali-2026

locations:
  - location-seminyak

events: []

tasks:
  - task-compare-villas

artifacts:
  - artifact-villa-spreadsheet

decisions:
  - decision-stay-seminyak

## Source Bulletins

- bulletin-2026-06-01-001
```

---

## 14.5 Trip Entity

Trips are first-class planning objects.

```markdown
---
entity_id: trip-bali-2026
entity_type: trip
display_name: Bali 2026
status: planning
---

# Bali 2026

## Summary

Family trip currently in planning.

## Current State

Accommodation evaluation is underway. Seminyak is the current preferred accommodation area.

## Travellers

- contact-7f3a91
- contact-91bd22

## Location Chain

1. location-perth
2. location-denpasar
3. location-seminyak
4. location-perth

## Travel Dates

TBD

## Constraints

- School holidays.
- Direct flights preferred.
- Family-friendly accommodation preferred.

## Decisions

- decision-stay-seminyak

## Open Tasks

- task-compare-villas

## Artifacts

- artifact-villa-spreadsheet

## Related Entities

contacts:
  - contact-7f3a91
  - contact-91bd22

groups:
  - group-bali-travellers

channels:
  - channel-whatsapp-bali-trip

trips: []

locations:
  - location-perth
  - location-denpasar
  - location-seminyak

events: []

tasks:
  - task-compare-villas

artifacts:
  - artifact-villa-spreadsheet

decisions:
  - decision-stay-seminyak

## Timeline

### 2026-06-01

- Seminyak became the preferred accommodation area.
- Villa comparison spreadsheet was created.

## Source Bulletins

- bulletin-2026-06-01-001
```

---

## 14.6 Location Entity

```markdown
---
entity_id: location-seminyak
entity_type: location
display_name: Seminyak
location_type: destination
status: active
---

# Seminyak

## Summary

Possible accommodation location for Bali 2026.

## Current State

Preferred accommodation area under evaluation.

## Related Entities

contacts: []

groups:
  - group-bali-travellers

channels:
  - channel-whatsapp-bali-trip

trips:
  - trip-bali-2026

locations: []

events: []

tasks:
  - task-compare-villas

artifacts:
  - artifact-villa-spreadsheet

decisions:
  - decision-stay-seminyak

## Source Bulletins

- bulletin-2026-06-01-001
```

---

## 14.7 Event Entity

Events are time-bound occurrences.

```markdown
---
entity_id: event-bali-arrival
entity_type: event
display_name: Bali Arrival
status: planned
date: TBD
---

# Bali Arrival

## Summary

Arrival event for Bali 2026.

## Current State

Planned but not yet scheduled.

## Date

TBD

## Related Entities

contacts:
  - contact-7f3a91
  - contact-91bd22

groups:
  - group-bali-travellers

channels:
  - channel-whatsapp-bali-trip

trips:
  - trip-bali-2026

locations:
  - location-denpasar

events: []

tasks: []

artifacts: []

decisions: []

## Source Bulletins
```

---

## 14.8 Task Entity

```markdown
---
entity_id: task-compare-villas
entity_type: task
display_name: Compare Villas
status: open
owner: contact-7f3a91
due_date: null
---

# Compare Villas

## Summary

Compare accommodation options for Bali 2026.

## Current State

Open.

## Owner

contact-7f3a91

## Context

A spreadsheet was created to compare villa options.

## Related Entities

contacts:
  - contact-7f3a91

groups:
  - group-bali-travellers

channels:
  - channel-whatsapp-bali-trip

trips:
  - trip-bali-2026

locations:
  - location-seminyak

events: []

tasks: []

artifacts:
  - artifact-villa-spreadsheet

decisions:
  - decision-stay-seminyak

## Source Bulletins

- bulletin-2026-06-01-001
```

---

## 14.9 Artifact Entity

Artifacts represent workspace outputs.

They answer:

- What file exists?
- Why was it created?
- Who created it?
- What trip, task, event, or group does it support?
- Where is it stored?

```markdown
---
entity_id: artifact-villa-spreadsheet
entity_type: artifact
display_name: Bali Villa Comparison Spreadsheet
artifact_type: spreadsheet
workspace_path: workspace/trips/bali/villas.xlsx
created_by: contact-7f3a91
created_for: trip-bali-2026
status: active
---

# Bali Villa Comparison Spreadsheet

## Purpose

Compare villa options for Bali 2026.

## Current State

Active.

## Workspace Path

workspace/trips/bali/villas.xlsx

## Created By

contact-7f3a91

## Created For

trip-bali-2026

## Related Entities

contacts:
  - contact-7f3a91

groups:
  - group-bali-travellers

channels:
  - channel-whatsapp-bali-trip

trips:
  - trip-bali-2026

locations:
  - location-seminyak

events: []

tasks:
  - task-compare-villas

artifacts: []

decisions:
  - decision-stay-seminyak

## Source Bulletins

- bulletin-2026-06-01-001
```

---

## 14.10 Decision Entity

Decisions represent settled outcomes.

Suggestions are not decisions.

```markdown
---
entity_id: decision-stay-seminyak
entity_type: decision
display_name: Stay In Seminyak
decision_type: trip_location
status: active
decided_for: trip-bali-2026
---

# Stay In Seminyak

## Summary

Accommodation search should focus on Seminyak.

## Decision

Bali 2026 should focus accommodation search on Seminyak.

## Status

Active.

## Reasoning

Seminyak is currently preferred by the trip group.

## Related Entities

contacts: []

groups:
  - group-bali-travellers

channels:
  - channel-whatsapp-bali-trip

trips:
  - trip-bali-2026

locations:
  - location-seminyak

events: []

tasks:
  - task-compare-villas

artifacts:
  - artifact-villa-spreadsheet

decisions: []

## Source Bulletins

- bulletin-2026-06-01-001
```

---

# 15. Aliases

Aliases assist with entity resolution.

Aliases are not authoritative for contacts.

Contacts Database remains authoritative.

Example:

```yaml
Michael:
  contact_id: contact-7f3a91

Mike:
  contact_id: contact-7f3a91

M Cleaver:
  contact_id: contact-7f3a91
```

Aliases may also apply to trips, locations, groups, channels, tasks, artifacts, and decisions.

Example:

```yaml
Bali trip:
  entity_id: trip-bali-2026

Villa sheet:
  entity_id: artifact-villa-spreadsheet
```

---

# 16. Indexes

Indexes are derived lookup structures.

Examples:

```text
entity-map.yml
claim-map.yml
reverse-links.yml
artifact-map.yml
trip-map.yml
channel-map.yml
```

Indexes support fast lookup but are not authoritative.

They can be deleted and rebuilt.

---

# 17. Summaries

Summaries provide memory compaction.

Examples:

```text
summaries/monthly/
summaries/trips/
summaries/groups/
summaries/channels/
```

Example:

```text
summaries/trips/trip-bali-2026.md
```

Summaries are derived.

They preserve references to source bulletins and claims.

---

# 18. Policies

Policies define system behavior.

Examples:

```text
policies/privacy.md
policies/retention.md
policies/claim-lifecycle.md
policies/sharing-rules.md
policies/memory-worthiness.md
```

---

# 19. Privacy Model

Every bulletin and claim contains visibility metadata.

Example:

```yaml
visibility: group
scope:
  - group-bali-travellers
```

Possible visibility levels:

```text
private
contact
group
channel
public
```

The retrieval system must enforce privacy before reading document bodies.

---

## 19.1 Query Context

All memory queries execute within a context.

Example:

```yaml
actor: contact-7f3a91
channel_id: channel-whatsapp-bali-trip
allowed_scopes:
  - public
  - contact-7f3a91
  - group-bali-travellers
  - channel-whatsapp-bali-trip
```

---

## 19.2 Privacy-Aware Retrieval

```text
Candidate Discovery
    ↓
Read Frontmatter Only
    ↓
Visibility Filter
    ↓
Scope Filter
    ↓
Read Allowed Files
    ↓
LLM Synthesis
```

Raw ripgrep results must not be passed directly to the LLM.

Candidate files must be filtered using frontmatter first.

---

# 20. Retrieval Workflow

```text
User Query
    ↓
Entity Extraction
    ↓
Entity Resolution
    ↓
Candidate Discovery
    ↓
Visibility Filtering
    ↓
Scope Filtering
    ↓
Entity Retrieval
    ↓
Graph Expansion
    ↓
Bulletin Expansion
    ↓
LLM Synthesis
```

---

# 21. Memory Query Execution Prompt

Use the following prompt for retrieval agents.

---

## Prompt

You are a memory retrieval agent operating against the Agent Memory System.

Your objective is to answer the user's question using the minimum amount of memory required while maintaining provenance and privacy.

The memory system contains:

- Channels
- Contacts
- Groups
- Trips
- Locations
- Events
- Tasks
- Artifacts
- Decisions
- Claims
- Bulletins

Bulletins are the source of truth.

Entity documents are derived summaries.

All relationships use canonical IDs.

Contacts use IDs from the Contacts Database.

---

## Retrieval Rules

1. Do not search bulletins first.
2. Start with entity documents.
3. Read the minimum number of files.
4. Use Related Entities for graph traversal.
5. Do not perform broad searches unless entity retrieval fails.
6. Only read bulletins when provenance or missing detail requires it.
7. Respect visibility and scope restrictions.
8. Never retrieve content outside the current query scope.
9. Prefer current-state summaries over historical records.
10. Report uncertainty and conflicts clearly.

---

## Query Procedure

### Step 1: Understand User Intent

Determine:

```yaml
query_type:
likely_entities:
```

Questions to consider:

- Is this about a trip?
- Is this about a contact?
- Is this about a group?
- Is this about a channel?
- Is this about a task?
- Is this about an artifact?
- Is this about a decision?
- Is this about a location?

---

### Step 2: Resolve Entities

Convert names into IDs.

Use:

- Contacts Database
- aliases
- entity indexes
- existing entity documents

Output:

```yaml
resolved_entities:
```

Example:

```text
Michael
```

becomes:

```text
contact-7f3a91
```

---

### Step 3: Determine Retrieval Roots

Select the most likely starting entities.

Examples:

```text
Trip question      → Trip
Group question     → Group
Artifact question  → Artifact
Channel question   → Channel
Task question      → Task
Location question  → Location
Decision question  → Decision
```

Output:

```yaml
retrieval_roots:
```

---

### Step 4: Retrieve Root Entity Documents

Read root entities first.

Extract:

```yaml
facts_found:
related_entities:
source_bulletins:
```

---

### Step 5: Traverse Related Entities

Only expand if needed.

Examples:

```text
Trip
  → Tasks
  → Artifacts
  → Decisions
  → Locations

Channel
  → Group
  → Trip
  → Recent Bulletins

Artifact
  → Trip
  → Task
  → Source Bulletin
```

Output:

```yaml
expanded_entities:
```

---

### Step 6: Read Supporting Bulletins

Read bulletins only if:

- more detail is needed
- provenance is required
- entity docs conflict
- entity docs are insufficient
- current state is unclear

Prefer newer relevant bulletins first.

Output:

```yaml
bulletins_read:
```

---

### Step 7: Synthesize Answer

Answer concisely.

Include:

- current state
- active tasks
- decisions
- relevant artifacts
- relevant dates
- provenance references where useful

When uncertain:

- say what is uncertain
- identify conflicting claims
- cite supporting bulletins or source docs

---

## Example Query: Trip Tasks

User:

```text
What is left to do for Bali?
```

Procedure:

```text
Resolve Bali → trip-bali-2026
Read trip-bali-2026
Expand to related tasks
Read open task documents
Return open tasks and blockers
```

---

## Example Query: Artifact

User:

```text
What spreadsheet did we make for accommodation?
```

Procedure:

```text
Resolve accommodation context → trip-bali-2026
Read trip-bali-2026
Expand to related artifacts
Read artifact-villa-spreadsheet
Return path, purpose, and related task
```

---

## Example Query: Channel Summary

User:

```text
What happened in the Bali WhatsApp chat this week?
```

Procedure:

```text
Resolve Bali WhatsApp chat → channel-whatsapp-bali-trip
Read channel document
Read recent allowed bulletins
Summarize decisions, tasks, artifacts, and important changes
Ignore conversational noise
```

---

# 22. Rebuildability

The system must support:

```bash
memory rebuild --all
```

Procedure:

1. Read all bulletins.
2. Regenerate claims.
3. Regenerate aliases.
4. Regenerate indexes.
5. Regenerate entity documents.
6. Regenerate summaries.

Only bulletins survive rebuilds.

Everything else is derived.

---

# 23. Compaction and Archival

As bulletins accumulate, historical memory should be compacted.

Suggested strategy:

```text
Active entities:
  keep detailed entity docs hot

Recent bulletins:
  keep fully indexed

Old bulletins:
  retain immutable source records

Summaries:
  generate monthly, trip, group, and channel summaries
```

Example summary files:

```text
summaries/monthly/2026-06.md
summaries/trips/trip-bali-2026.md
summaries/groups/group-bali-travellers.md
summaries/channels/channel-whatsapp-bali-trip.md
```

Compaction must preserve references back to source bulletins.

---

# 24. Embeddings

Embeddings are intentionally excluded from the core architecture.

Primary retrieval should use:

```text
Markdown
Ripgrep
Canonical IDs
Entity documents
Related Entities graph traversal
LLM reasoning
```

Embeddings may later be introduced as a secondary fuzzy-recall layer.

Useful future cases:

```text
"What was that beach place Sarah mentioned?"
"Find the document about accommodation options."
"What did we discuss about school holiday travel?"
```

Embeddings should not be required for correctness, provenance, or privacy.

---

# 25. Operational Commands

Suggested CLI commands:

```bash
memory submit-bulletin --channel channel-whatsapp-bali-trip --file draft.md

memory process-staging

memory query "what is left for Bali?" --actor contact-7f3a91 --channel channel-whatsapp-bali-trip

memory rebuild --all

memory rebuild --entity trip-bali-2026

memory validate

memory compact --month 2026-06
```

---

# 26. Final Architecture Summary

The memory system should be understood as:

```text
Channels raise bulletins.

Bulletins are immutable source records.

Claims are atomic extracted memories.

Entity documents are derived current-state views.

Related Entities make the memory navigable as a graph.

Contacts are resolved through the Contacts Database.

Privacy is enforced before document bodies are read.

Trips, groups, channels, and artifacts are first-class navigation roots.

Everything except bulletins can be rebuilt.
```