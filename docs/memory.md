# Memory Wiki System

The memory wiki gives Cyborg a persistent, structured knowledge base that survives across conversations. Without memory, every session starts from scratch -- the LLM has no recollection of facts, people, or preferences discussed in prior exchanges. The memory system closes this gap by recording useful information during and after conversations, then automatically surfacing it in future prompts.

Memory is organized as a file-based wiki stored under the workspace directory. A lightweight entity index is always injected into the system prompt, so the assistant knows what it knows without extra tool calls. When it needs detail, it uses on-demand tools to read, search, or graph-traverse entries. When it learns something worth remembering, it writes a bulletin. A background dream pipeline curates bulletins into structured entity documents via an intermediate claim-extraction step.

The data model follows a four-stage pipeline:

```
channel  -->  bulletin  -->  claim  -->  entity document
(source)     (record)     (atom)     (derived view)
```

## Architecture Overview

```
                              Prompt Assembly
                              ==============
                              +-----------+
                              | SOUL.md   |
                              | IDENTITY  |
                              | AGENTS.md |
                              | USER.md   |
                              | Skills    |
                              +-----+-----+
                                    |
                     +--------------+--------------+
                     |                             |
              +------+------+              +-------+-------+
              | Memory Index |              | Grounding     |
              | (entities)   |              | Rules         |
              +------+------+              +---------------+
                     |
              +------+------+
              |  System     |
              |  Prompt     |
              +------+------+
                     |
         +-----------+-----------+
         |   Session History     |
         +-----------+-----------+
                     |
         +-----------+-----------+
         |   User Message        |
         +-----------+-----------+
                     |
              +------+------+
              |  LLM Dispatch|
              |  + Tools     |
              +------+------+
                     |
         +-----------+-----------+-----------+-----------+
         |           |           |           |           |
    memory_write  memory_read  memory_search memory_browse memory_graph
         |           |           |             |           |
         v           |           v             v           v
   +----------+      |    +------------+ +----------+ +---------+
   | bulletin |      |    | LLM-powered| | entity   | | graph   |
   | queue    |      |    | semantic   | | listing  | | traverse|
   +----+-----+      |    | search     | +----------+ +---------+
        |            |    +------+-----+
        |            |           |
        |            |     search logged to
        |            |     memory_search_log
        |            |
        |            v
        |    +--------------------------+
        |    | memory/                  |
        |    |  entities/               |
        |    |    contacts/             |
        |    |    groups/               |
        |    |    channels/             |
        |    |    trips/ ...            |
        |    |  bulletins/              |
        |    |  claims/                 |
        |    |  indexes/                |
        |    |  aliases/                |
        |    +--------------------------+
        |
        v
  Heartbeat (SessionIdleSummaryTask)
        |
        +--> generate_summary() -- store to DB
        |
        +--> build_generator_input() from transcript
        |    --> generate_bulletin() via LLM
        |    --> write_bulletin() to memory/bulletins/
        |
        v
  MemoryService.run_dream()
        |
        v
  For each pending bulletin:
    1. extract_claims_from_bulletin() --> write claims to memory/claims/
    2. _update_entities_from_claims()  --> LLM writes/updates entity docs
    3. rebuild indexes + aliases
    4. mark bulletin digested
```

## File-Based Storage

All memory data lives under `<workspace_dir>/memory/`. There is no database table for entries -- everything is a markdown file on disk, versioned alongside the workspace. Each file uses YAML frontmatter for metadata.

### Directory Structure

```
memory/
  bulletins/                    # Immutable source records
    2026/                       # Year
      05/                       # Month
        bulletin-2026-05-31-a1b2c3.md
      06/
        bulletin-2026-06-01-d4e5f6.md
  claims/                       # Extracted atomic claims
    claim-2026-05-31-001.md
    claim-2026-06-01-001.md
  entities/                     # Derived entity documents
    contacts/                   # People
      contact-7c9f0fd7.md
      contact-a3b4c5d6.md
    groups/                     # WhatsApp groups, etc.
      group-12036342829458.md
    channels/                   # Communication channels
      channel-whatsapp-group-12036342829458.md
    trips/                      # Travel plans
      trip-bali-2026.md
    locations/                  # Places
      location-seminyak.md
    events/                     # Calendar events
    tasks/                      # Action items
    artifacts/                  # Documents, spreadsheets
    decisions/                  # Decisions made
  indexes/                      # Derived lookup structures
    entity-map.yml              # entity_id -> {type, display_name, path}
    reverse-links.yml           # entity_id -> [referencing entity_ids]
  aliases/                      # Name-to-ID mapping
    aliases.yml                 # display_name -> entity_id
  summaries/                    # Reserved for summary caching
  policies/                     # Reserved for access policies
```

The `memory/` directory and its subdirectory structure are created automatically by `MemoryService.ensure_memory_structure()` the first time the system starts or when the prompt assembler runs.

### Entity Document Format

Each entity document is a markdown file with YAML frontmatter:

```markdown
---
entity_id: contact-7c9f0fd7
entity_type: contact
display_name: Alice Johnson
status: active
contact_source: contacts_db
---

# Alice Johnson

## Summary

Software engineer at TechCorp. Prefers pour-over coffee.

## Current State

Working on Project Alpha. UTC+8 timezone.

## Related Entities

contacts: []
groups: [group-12036342829458]
channels: [channel-whatsapp-group-12036342829458]
trips: [trip-bali-2026]
locations: []
events: []
tasks: [task-compare-villas]
artifacts: []
decisions: []

## Timeline

- 2026-05-30: Joined Bali trip planning group.
- 2026-05-31: Volunteered to compare villas.

## Source Bulletins

- bulletin-2026-05-31-a1b2c3
```

The `Related Entities` section is mandatory and contains all typed lists even when empty. This enables graph traversal via the `memory_graph` tool.

### Bulletin Format

Bulletins are immutable source records with frontmatter tracking provenance:

```markdown
---
id: bulletin-2026-05-31-a1b2c3
created_at: "2026-05-31T10:30:00+00:00"
channel_id: channel-whatsapp-group-12036342829458
source_type: session_transcript_range
source_id: agent:main:whatsapp:group:12036342829458
visibility: group
scope:
  - public
  - group-12036342829458
entities:
  contacts:
    - id: contact-7c9f0fd7
      display_name: Alice Johnson
  channels:
    - id: channel-whatsapp-group-12036342829458
digested: true
---

# Update

Group decided to stay in Seminyak for the Bali trip. Alice volunteered to compare villas.
```

Once digested by the dream pipeline, the `digested: true` flag is set in the frontmatter rather than moving the file.

### Claim Format

Claims are atomic typed propositions extracted from bulletins:

```markdown
---
id: claim-2026-05-31-001
type: decision
subject_id: trip-bali-2026
predicate: accommodation_focus
object_id: location-seminyak
status: active
source_bulletins:
  - bulletin-2026-05-31-a1b2c3
visibility: group
scope:
  - group-12036342829458
created_at: "2026-05-31T12:00:00+00:00"
superseded_by: []
---

Bali 2026 accommodation search should focus on Seminyak.
```

Claim types: `fact`, `preference`, `constraint`, `decision`, `task`, `availability`, `booking`, `artifact`, `relationship`, `private_note`.

### Derived Indexes

Three derived index files are maintained by `index_service.rebuild_all()`:

| File | Purpose |
|------|---------|
| `indexes/entity-map.yml` | Maps entity_id to `{entity_type, display_name, path}` for quick lookups |
| `indexes/reverse-links.yml` | Maps entity_id to list of entity_ids that reference it (for graph traversal) |
| `aliases/aliases.yml` | Maps display names (and lowercase variants) to entity IDs for name resolution |

These are rebuilt automatically after every dream cycle and after entity writes.

## Authoring Memory Entries

### Real-Time: memory_write Tool

During an active conversation, the LLM can use the `memory_write` tool to queue a bulletin. This is the fastest path -- the assistant decides a fact is worth remembering and writes it in the same turn. The bulletin lands in `memory/bulletins/YYYY/MM/` awaiting the dream process.

Parameters:

| Parameter | Description |
|-----------|-------------|
| `content` | Markdown body describing what to remember |
| `channel_id` | Optional channel association (defaults to session's channel) |
| `visibility` | Privacy level: `private`, `contact`, `group`, `channel`, `public` |

The tool derives the channel_id from the session key via `resolve_channel_id()`, validates the input, and calls `write_bulletin()` which generates a unique ID (`bulletin-YYYY-MM-DD-xxxxxx`), attaches metadata (source session, timestamps), and writes the file. The workspace's `write_file` tool is guarded to reject writes into `memory/` -- all modifications must go through `memory_write` to keep indexes consistent.

### Post-Session: Bulletin Generation via Heartbeat

After a conversation goes idle, the heartbeat system generates a summary and produces a bulletin from the transcript. The flow is:

1. `SessionIdleSummaryTask` (registered in `heartbeat.py`) runs on each heartbeat cycle.
2. It calls `SessionSummaryService.find_idle_sessions()` to detect sessions with no recent activity beyond the configured idle threshold.
3. For each idle session, it fetches messages and participants, then calls `generate_summary()` to produce `summary_text` and `topics`.
4. The summary is stored in the `session_summaries` database table.
5. Independently, the heartbeat task builds a `BulletinGeneratorInput` from the transcript using `build_generator_input()`:
   - Derives `channel_id`, `visibility`, `scope`, and `channel_type` from the session key.
   - Includes the last 50 messages (truncated to 500 chars each) as the transcript.
6. Calls `generate_bulletin()` which invokes an LLM with the `BULLETIN_GENERATION_PROMPT` system prompt.
7. The LLM decides whether to create a bulletin (using the memory-worthiness rules in the prompt) or to decline with a reason.
8. The response is validated via `validate_draft_bulletin()`.
9. If valid and `create_bulletin: true`, the bulletin is written to disk.

The bulletin generator uses a detailed system prompt that enforces memory-worthiness rules (only decisions, tasks, preferences, constraints, bookings, artifacts, trips, locations, availability changes, relationships, and important facts) and rejects conversational noise (greetings, jokes, emoji reactions, duplicates).

After all session summaries are processed, the heartbeat task runs `MemoryService.run_dream()` to curate pending bulletins into structured entries.

## The Dream Process

The dream process is the curation pipeline that transforms raw bulletins into structured entity documents. It runs automatically at the end of each heartbeat cycle. The pipeline has three stages per bulletin:

### Stage 1: Claim Extraction

`extract_claims_from_bulletin()` sends the bulletin to an LLM with the `CLAIM_EXTRACTION_PROMPT` system prompt. The LLM returns a JSON array of atomic claim objects, each with:
- `type` (fact, preference, constraint, decision, task, etc.)
- `subject_id` and `object_id` (canonical entity IDs)
- `predicate` (a verb phrase like `accommodation_focus`)
- `body` (human-readable statement)
- `visibility` and `scope` (inherited from the bulletin)
- `source_bulletin_id` (provenance link)

Entity IDs in claims are normalized via `normalize_entity_id()` which handles raw UUIDs, contact ID formats, and artifact paths. Claims are written to `memory/claims/` as individual markdown files.

### Stage 2: Entity Document Update

`_update_entities_from_claims()` collects all entity IDs referenced in the claims, reads any existing entity documents, and sends everything to an LLM with the `ENTITY_UPDATE_PROMPT` system prompt. The LLM returns a JSON array of write operations, each containing the full entity document (frontmatter + markdown body) including:
- Summary and Current State sections (prioritizing latest information)
- Related Entities section with all typed lists (mandatory)
- Timeline of recent events
- Source Bulletins list for provenance

The entity update agent resolves contact names from the database, normalizes entity IDs, and handles both creates and updates. Existing entity documents are merged with new claims, with newer information taking precedence.

### Stage 3: Index Rebuild

After all bulletins are processed, `rebuild_indexes()` rebuilds the three derived index files (entity-map, reverse-links, aliases) and the compact text index used for prompt injection.

### Bulletin Digestion

Processed bulletins are marked `digested: true` in their frontmatter (via `_mark_digested()`). They remain in place for provenance and can be re-digested via the dashboard.

### Dream Log

Every dream run is logged to the `memory_dream_log` database table with the number of bulletins processed, entries created, claims extracted, per-bulletin operation details, duration, and status.

## Retrieval

### Lightweight Index (Always in Prompt)

The `prompt_assembler` module integrates memory during prompt construction in `load_workspace_prompt()`. The process:

1. `ensure_memory_structure()` is called to guarantee the directory exists.
2. `build_memory_index_text()` scans `memory/entities/` and builds a compact listing of all entities grouped by type, showing display names and truncated summaries (max 80 chars).
3. A `## Memory` section is appended to the system prompt containing tool usage instructions and the entity index.

This is a zero-overhead path -- no tool call, no extra latency. The assistant starts every turn knowing what entities exist.

Example index injection:

```
## Memory

You have persistent memory with these tools:
- **memory_search(query, entity_type?)** -- Always start here.
- **memory_read(entity_id)** -- Read a specific entity.
- **memory_browse(entity_type)** -- List all entities of a type.
- **memory_write(content, channel_id?, visibility?)** -- Write a new bulletin.
- **memory_graph(entity_id, depth?)** -- Explore related entities.

Entity types: contacts, groups, channels, trips, locations, events, tasks, artifacts, decisions.

**contacts**: Alice Johnson -- Software engineer at TechCorp, Bob Smith -- Prefers dark roast coffee
**groups**: Bali Trip Group -- Planning trip to Bali in June 2026
**trips**: trip-bali-2026 -- Group trip to Bali, dates June 2026
```

### memory_read Tool

Reads a single entity by canonical ID. Returns the full markdown content (frontmatter + body). Checks that the entity exists. Returns an error JSON if not found.

### memory_search Tool

Semantic search across entity documents. The search is LLM-powered:

1. Collects all entity documents (or filtered to a specific `entity_type`) from `memory/entities/`.
2. Builds a catalog with truncated body text (300 chars per entry), entity IDs, types, and display names.
3. Sends the catalog and query to an LLM with a strict system prompt requesting JSON with `abstract` (1-2 sentence summary) and `results` (array of matched entries with index numbers and relevance explanations).
4. Maps index numbers back to entity IDs and paths.

Returns `{abstract, results}` where each result has `entity_id`, `entity_type`, `display_name`, `path`, and `relevance`. The assistant can use `memory_read` with the entity_id to get the full document.

Uses the model specified by `LLMDispatchService.memory_model` with temperature 0.0. Every search is logged to the `memory_search_log` database table.

### memory_browse Tool

Lists all entities of a given type. Returns a JSON array of `{entity_id, display_name, status}` sorted alphabetically by filename. Useful for exploring what exists in a category before searching.

### memory_graph Tool

Explores the memory graph around an entity. Reads the entity's Related Entities section, then loads each referenced entity to build a neighbor map. Returns the entity's metadata plus a dict of `{category: [neighbor_entities]}`. Currently supports depth=1 (immediate neighbors).

## Entity Resolution

The `entity_resolver` module handles mapping between different ID formats and display names:

- `canonical_contact_id(uuid)` -- Converts full UUIDs to `contact-{hex8}` format (e.g. `7c9f0fd7-6134-4495-aa8c-f04f11bc15e8` becomes `contact-7c9f0fd7`).
- `normalize_entity_id(entity_id, entity_type)` -- Normalizes any entity ID variant, handling raw UUIDs, slashes in artifact paths, and different contact ID formats.
- `resolve_contact(db, name_or_ref)` -- Resolves names, `{{contact:UUID|Name}}` template references, or raw UUIDs to canonical contact IDs using database lookups.
- `load_aliases(memory_dir)` -- Loads the aliases index for name-to-ID resolution.

Channel IDs are derived from session keys via `channels.resolve_channel_id()`:

| Session Key Format | Channel ID |
|---|---|
| `agent:main:whatsapp:group:120363...` | `channel-whatsapp-group-120363...` |
| `agent:main:whatsapp:dm:61456224867` | `channel-whatsapp-dm-61456224867` |
| `agent:main:email:thread-id` | `channel-email-thread-id` |

Visibility and scope are also derived from the session key:
- Group chats get `visibility: group` with a `group-{id}` scope.
- DMs get `visibility: contact` with the contact's ID in scope.
- Other sessions default to `visibility: private`.

## CLI Commands

### cyborg memory seed

Regenerates all memory from session history using the bulletin generator. Useful for initial setup or full regeneration.

```bash
# Dry run -- see what would be processed without calling the LLM
cyborg memory seed --dry-run

# Full seed -- generates bulletins and runs dream pipeline
cyborg memory seed
```

The command (`seed_from_history()`) follows this process:

1. Backs up any old `core/` directory to `core.v1.bak/` (v1 legacy).
2. Creates the v6 directory structure.
3. Queries all distinct session keys from `session_messages`, ordered by first message.
4. Loads known contacts from the database for entity resolution.
5. For each session with 3+ messages:
   - Builds a transcript from messages (with sender names resolved).
   - Splits long transcripts into 8000-char chunks.
   - Calls `generate_bulletin()` with the transcript and known entity hints.
   - Validates the response and writes the bulletin if valid.
6. Runs the dream pipeline on all generated bulletins.

### cyborg memory rebuild

Rebuilds derived data from bulletins.

```bash
# Rebuild indexes only for a specific entity
cyborg memory rebuild --entity contact-7c9f0fd7

# Full rebuild: clear claims and indexes, re-process all bulletins
cyborg memory rebuild --all
```

### cyborg memory validate

Validates memory structure by checking that all entity documents have required frontmatter fields (`entity_id`, `entity_type`, `display_name`).

### cyborg memory query

Queries memory with a natural language question. Optionally filters by entity type, actor, or channel.

```bash
cyborg memory query "What is left to do for Bali?"
cyborg memory query "Alice" --type contacts
```

## Search Logging

Every `memory_search` call (from tool or dashboard) is logged to the `memory_search_log` table. Schema from `schemas/300_memory_search_log.sql`:

```sql
CREATE TABLE IF NOT EXISTS memory_search_log (
    id TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    results_json TEXT NOT NULL DEFAULT '[]',
    session_key TEXT,
    result_count INTEGER NOT NULL DEFAULT 0,
    latency_seconds REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Fields:

| Column | Description |
|--------|-------------|
| `id` | UUID primary key |
| `query` | The search query string |
| `results_json` | Full JSON response (abstract + results array) |
| `session_key` | Session that initiated the search (null for dashboard) |
| `result_count` | Number of results returned |
| `latency_seconds` | Wall-clock time for the search operation |
| `created_at` | Timestamp |

Logging failures are caught silently to avoid disrupting the search response.

## Dream Logging

Every dream run is logged to the `memory_dream_log` table. Schema from `schemas/301_memory_dream_log.sql` and extended by `schemas/302_memory_dream_log_raw_response.sql`:

```sql
CREATE TABLE IF NOT EXISTS memory_dream_log (
    id TEXT PRIMARY KEY,
    bulletins_processed INTEGER NOT NULL DEFAULT 0,
    entries_created INTEGER NOT NULL DEFAULT 0,
    bulletin_slugs TEXT NOT NULL DEFAULT '[]',
    operations_json TEXT NOT NULL DEFAULT '[]',
    raw_response TEXT,
    duration_seconds REAL,
    status TEXT NOT NULL DEFAULT 'completed',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

The dream log is written by the heartbeat task after `run_dream()` completes, capturing the full result including per-bulletin operation details.

## Dashboard Memory Page

The dashboard includes a memory page at `/memory` with these features:

1. **Stats header** -- Total entity count and per-type counts, pulled from `GET /api/memory/stats`.
2. **Live search** -- An input field that calls `GET /api/memory/search?q=...` and displays results with abstract, relevance explanations, and latency. Results are clickable to open a content viewer.
3. **Pending bulletins** -- Shows all undigested bulletins with their source session, source type, and content preview. Each bulletin is clickable to view full content.
4. **Dream log** -- A feed of dream runs showing status, bulletins consumed, claims extracted, entries created, duration, and per-bulletin operation details. Each run is expandable to show:
   - The consumed bulletins with content (fetched via `POST /api/memory/digested`).
   - Per-bulletin breakdown of claims and entity operations.
   - The raw LLM response for debugging.
   - A "re-digest" button to re-process a bulletin through the dream pipeline.
5. **Validate (lint)** -- A button (with confirmation) to trigger `POST /api/memory/lint` which validates all entity documents for required fields.
6. **Content viewer** -- An inline panel that loads any memory file's content via `GET /api/workspace/file?path=...`.

Dashboard API endpoints (defined in `routers/dashboard_api.py`):

| Endpoint | Description |
|----------|-------------|
| `GET /api/memory/stats` | Entity counts, per-type stats, pending bulletins, last dream time |
| `GET /api/memory/search?q=...` | Search entities, log result, return with latency |
| `GET /api/memory/searches` | Last 100 search log entries with parsed results |
| `GET /api/memory/bulletins` | Current pending (undigested) bulletins |
| `GET /api/memory/dreams` | Last 20 dream log entries |
| `GET /api/memory/category/{category}` | Entities in a specific type directory |
| `POST /api/memory/digested` | Fetch content of specific bulletins by slug |
| `POST /api/memory/redigest` | Re-process a bulletin through the dream pipeline |
| `POST /api/memory/lint` | Validate all entity documents |

All endpoints are protected by the dashboard secret (Bearer token or `?secret=` query parameter).

## Key Source Files

| File | Purpose |
|------|---------|
| `services/memory/service.py` | Core service: bulletin/entity CRUD, dream pipeline, search, reflection, validation |
| `services/memory/models.py` | Data models (Bulletin, Claim, EntityDocument, EntityRef, QueryContext, frontmatter helpers) |
| `services/memory/claim_service.py` | Claim extraction from bulletins, claim CRUD, active claim queries |
| `services/memory/entity_resolver.py` | Entity ID normalization, contact resolution, alias loading |
| `services/memory/channels.py` | Session key to channel ID/visibility/scope derivation |
| `services/memory/index_service.py` | Derived index building (entity-map, reverse-links, aliases, prompt index text) |
| `services/memory/prompts.py` | LLM system prompts for bulletin generation, claim extraction, entity update, retrieval |
| `services/memory/bulletin_generator.py` | Transcript-to-bulletin LLM pipeline with input construction and output validation |
| `services/memory/seed.py` | Bulk history regeneration from session messages |
| `services/memory_tools.py` | LLM function-call tools (memory_write, memory_read, memory_search, memory_browse, memory_graph) |
| `services/prompt_assembler.py` | Injects memory index and tool descriptions into system prompt |
| `services/workspace_tools.py` | Guards `memory/` directory from direct write_file access |
| `services/session_summary_service.py` | Generates summaries from session history |
| `heartbeat.py` | SessionIdleSummaryTask triggers bulletin generation + dream after summaries |
| `cli.py` | CLI commands: `cyborg memory seed/rebuild/validate/query` |
| `schemas/300_memory_search_log.sql` | Database schema for search logging |
| `schemas/301_memory_dream_log.sql` | Database schema for dream run logging |
| `schemas/302_memory_dream_log_raw_response.sql` | Adds raw_response column to dream log |
| `routers/dashboard_api.py` | Dashboard API endpoints for memory stats, search, bulletins, dreams |
| `ui_app/src/routes/memory/index.tsx` | Dashboard memory page UI component |
