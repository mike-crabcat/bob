# Memory System

The memory system gives Cyborg a persistent, structured knowledge base that survives across conversations. Without memory, every session starts from scratch -- the LLM has no recollection of facts, people, or preferences discussed in prior exchanges. The memory system closes this gap by recording useful information during and after conversations, then surfacing it on demand via retrieval tools.

Memory is backed by SQLite tables in the main database. A lightweight memory section describing the available tools is injected into the system prompt. When the assistant needs detail, it uses on-demand tools to read, search, or graph-traverse entries. When it learns something worth remembering, it writes a bulletin. A background dream pipeline curates bulletins into structured entity documents via an intermediate claim-extraction step.

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
              | Memory      |              | Grounding     |
              | (tools only)|              | Rules         |
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
        |    +------------------------------------------+
        |    | SQLite tables:                           |
        |    |   memory_bulletins                       |
        |    |   memory_bulletin_entities                |
        |    |   memory_claims                          |
        |    |   memory_entities                        |
        |    |   memory_entity_relations                |
        |    |   memory_aliases                         |
        |    |   memory_entity_bulletins                |
        |    |   memory_claim_bulletins                 |
        |    +------------------------------------------+
        |
        v
  Heartbeat (SessionIdleSummaryTask)
        |
        +--> _find_idle_sessions() -- query DB for
        |    sessions with no recent bulletin coverage
        |
        +--> build_generator_input() from transcript
        |    --> generate_bulletins() via LLM
        |    --> write_bulletin() INSERT into memory_bulletins
        |
        v
  MemoryService.run_dream()
        |
        v
  For each pending bulletin:
    1. extract_claims_from_bulletin() --> INSERT into memory_claims
    2. _update_entities_from_claims()  --> LLM writes/updates entity docs
    3. aliases + relations maintained inline by write_entity()
    4. mark bulletin digested (digested=1 in DB)
```

## SQLite-Backed Storage

All memory data lives in SQLite tables defined by schema migrations. Tables are created automatically at server startup. The schema is in `schemas/307_memory_tables.sql` and `schemas/311_entity_bulletin_links.sql`.

### memory_bulletins

Immutable source records. Each bulletin is a plain-text memory captured from a channel.

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PK | Auto-generated: `bulletin-YYYY-MM-DD-xxxxxx` |
| `created_at` | TEXT | ISO timestamp |
| `channel_id` | TEXT | Channel the bulletin came from |
| `source_type` | TEXT | e.g. `session`, `email`, `manual` |
| `source_id` | TEXT | Session key or thread ID |
| `session_id` | TEXT | Session identifier |
| `transcript_range_id` | TEXT | Transcript range identifier |
| `visibility` | TEXT | `private`, `contact`, `group`, `channel`, `public` |
| `scope` | TEXT | JSON array of scope IDs |
| `memory_types` | TEXT | JSON array of memory type tags |
| `confidence` | TEXT | `high`, `medium`, `low` |
| `requires_review` | INTEGER | Boolean flag |
| `review_reasons` | TEXT | JSON array of reason strings |
| `content` | TEXT | Plain-text bulletin body |
| `digested` | INTEGER | 0=pending, 1=processed by dream |

### memory_bulletin_entities

Normalized entity references extracted from each bulletin.

| Column | Type | Description |
|--------|------|-------------|
| `bulletin_id` | TEXT FK | References `memory_bulletins.id` |
| `category` | TEXT | Entity category (contacts, groups, etc.) |
| `entity_id` | TEXT | Referenced entity ID |
| `display_name` | TEXT | Display name at extraction time |
| `resolution_status` | TEXT | `known`, `unresolved`, `ambiguous`, `proposed`, `resolved` |
| `role` | TEXT | Optional role (e.g. `task_owner`, `participant`) |

### memory_claims

Atomic typed memories extracted from bulletins by the LLM.

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PK | Auto-generated |
| `type` | TEXT | `fact`, `preference`, `constraint`, `decision`, `task`, `availability`, `booking`, `artifact`, `relationship`, `private_note` |
| `subject_id` | TEXT | Canonical entity ID |
| `predicate` | TEXT | Verb phrase (e.g. `accommodation_focus`) |
| `object_id` | TEXT | Optional target entity ID |
| `status` | TEXT | `active`, `superseded`, `retracted`, `expired`, `disputed`, `archived` |
| `visibility` | TEXT | Privacy level |
| `scope` | TEXT | JSON array |
| `created_at` | TEXT | ISO timestamp |
| `superseded_by` | TEXT | JSON array of claim IDs |
| `source_bulletins` | TEXT | JSON array of bulletin IDs |
| `body` | TEXT | Human-readable statement |

### memory_entities

Derived current-state views optimized for retrieval. Written by the dream pipeline's entity update step.

| Column | Type | Description |
|--------|------|-------------|
| `entity_id` | TEXT PK | Canonical ID (e.g. `contact-7c9f0fd7`, `trip-bali-2026`) |
| `entity_type` | TEXT | `channel`, `contact`, `group`, `location`, `trip`, `event`, `task`, `artifact`, `decision` |
| `display_name` | TEXT | Human-readable name |
| `status` | TEXT | `active`, `archived` |
| `extra_frontmatter` | TEXT | JSON object of additional metadata |
| `body` | TEXT | Markdown body with Summary, Current State, Related Entities, Timeline sections |
| `source_bulletins` | TEXT | JSON array of bulletin IDs (accumulated deterministically) |
| `created_at` | TEXT | ISO timestamp |
| `updated_at` | TEXT | ISO timestamp |

The `body` column contains markdown with a standard structure: Summary, Current State, Related Entities (mandatory, typed lists), Timeline, and Source Bulletins. Frontmatter is used as a serialization format for LLM communication and tool rendering, not as a storage format.

### memory_entity_relations

Normalized related-entities graph. Replaces the old `reverse-links.yml` file.

| Column | Type | Description |
|--------|------|-------------|
| `source_entity_id` | TEXT FK | Source entity |
| `category` | TEXT | Relation category (contacts, groups, etc.) |
| `target_entity_id` | TEXT | Target entity |

### memory_aliases

Display-name to entity-ID lookup. Replaces the old `aliases/aliases.yml` file.

| Column | Type | Description |
|--------|------|-------------|
| `alias` | TEXT PK | Display name (case-sensitive and lowercase variants stored) |
| `entity_id` | TEXT FK | Referenced entity |

### Join Tables (schemas/311_entity_bulletin_links.sql)

`memory_entity_bulletins` and `memory_claim_bulletins` provide indexed many-to-many links between entities/claims and their source bulletins for fast provenance lookups.

## Authoring Memory Entries

### Real-Time: memory_write Tool

During an active conversation, the LLM can use the `memory_write` tool to queue a bulletin. This is the fastest path -- the assistant decides a fact is worth remembering and writes it in the same turn. The bulletin is INSERTed into the `memory_bulletins` table awaiting the dream process.

Parameters:

| Parameter | Description |
|-----------|-------------|
| `content` | Markdown body describing what to remember |
| `channel_id` | Optional channel association (defaults to session's channel) |
| `visibility` | Privacy level: `private`, `contact`, `group`, `channel`, `public` |

The tool derives the channel_id from the session key via `resolve_channel_id()`, validates the input, and calls `write_bulletin()` which generates a unique ID (`bulletin-YYYY-MM-DD-xxxxxx`), attaches metadata (source session, timestamps), and INSERTs into the database.

### Post-Session: Bulletin Generation via Heartbeat

After a conversation goes idle, the heartbeat system generates bulletins from the transcript. The flow is:

1. `SessionIdleSummaryTask` (registered in `heartbeat.py`) runs on each heartbeat cycle.
2. `_find_idle_sessions()` queries `session_messages` for sessions with no recent bulletin coverage, finding messages newer than the last bulletin's `session_range_end` and older than the idle threshold.
3. For each idle session, it fetches messages and participant names from the database.
4. Builds a `BulletinGeneratorInput` from the transcript using `build_generator_input()`:
   - Derives `channel_id` and `visibility` from the session key.
   - Includes the last 50 messages (truncated to 500 chars each).
5. Calls `generate_bulletins()` which invokes an LLM with the bulletin generation prompt.
6. The LLM decides whether to create bulletins (using memory-worthiness rules) or to decline.
7. For each generated bulletin text, calls `write_bulletin()` to INSERT into the database.

After all sessions are processed, the heartbeat task runs `MemoryService.run_dream()` to curate pending bulletins into structured entries.

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

Entity IDs in claims are normalized via `normalize_entity_id()` and reconciled via `reconcile_contact_id()` using the `ContactDirectory` for contact ID lookup. Claims are INSERTed into `memory_claims` via `write_claim()`.

### Stage 2: Entity Document Update

`_update_entities_from_claims()` groups claims by `subject_id`, then for each entity:
1. Loads the `ContactDirectory` and existing contact display names.
2. Reconciles contact IDs to canonical form.
3. Fetches new bulletin content from the database.
4. Reads any existing entity body.
5. Accumulates `source_bulletins` deterministically (union of existing + new, not from LLM).
6. Sends everything to an LLM with the `ENTITY_UPDATE_PROMPT` system prompt.
7. The LLM returns a JSON array of write operations with full entity content (frontmatter + markdown body).
8. Each entity is parsed and written to the database via `write_entity()`.

`write_entity()` also maintains derived data inline:
- Deletes and re-inserts rows in `memory_entity_relations` from the entity's Related Entities.
- Updates `memory_aliases` with the entity's display name (case-sensitive and lowercase).
- Inserts into `memory_entity_bulletins` join table for provenance.

### Stage 3: Index Maintenance

Aliases and relations are maintained inline by `write_entity()` during each entity write. `index_service.rebuild_all()` is available for bulk rebuild operations (used by `cyborg memory rebuild`).

### Bulletin Digestion

Processed bulletins are marked `digested = 1` in the `memory_bulletins` table via `_mark_digested()`. They remain in the database for provenance and can be re-digested via the dashboard.

### Dream Log

Every dream run is logged to the `memory_dream_log` database table with the number of bulletins processed, entity operations, per-bulletin operation details, duration, and status.

## Retrieval

### Memory Tool Descriptions (In System Prompt)

The `prompt_assembler` module injects a `## Memory` section into every system prompt describing the five memory tools. The full entity index is currently disabled (commented out in `prompt_assembler.py`) because the ~22KB+ dump was mostly noise. Agents discover entities on demand via `memory_search` and `memory_browse` rather than through a prompt dump.

### memory_read Tool

Reads a single entity by canonical ID. Queries `memory_entities` and `memory_entity_relations` to reconstruct the full entity document. Returns frontmatter-rendered markdown (via `serialize_frontmatter()`) for compatibility with tool callers.

### memory_search Tool

Semantic search across entity documents. The search is LLM-powered:

1. Queries all entities from `memory_entities` table (or filtered to a specific `entity_type`).
2. Builds a catalog with truncated body text (300 chars per entry), entity IDs, types, and display names.
3. Sends the catalog and query to an LLM with a strict system prompt requesting JSON with `abstract` (1-2 sentence summary) and `results` (array of matched entries with index numbers and relevance explanations).
4. Maps index numbers back to entity IDs.

Returns `{abstract, results}` where each result has `entity_id`, `entity_type`, `display_name`, and `relevance`.

Uses the model specified by `LLMDispatchService.memory_model` with temperature 0.0. Every search is logged to the `memory_search_log` database table.

### memory_browse Tool

Lists all entities of a given type. Queries `memory_entities` filtered by `entity_type`, sorted by `entity_id`. Returns a JSON array of `{entity_id, display_name, status}`.

### memory_graph Tool

Explores the memory graph around an entity. Queries `memory_entity_relations` for the entity's relations, then loads each referenced entity to build a neighbor map. Returns the entity's metadata plus a dict of `{category: [neighbor_entities]}`. Currently supports depth=1 (immediate neighbors).

## Entity Resolution

The `entity_resolver` module handles mapping between different ID formats and display names:

- `canonical_contact_id(uuid)` -- Converts full UUIDs to `contact-{hex8}` format (e.g. `7c9f0fd7-6134-4495-aa8c-f04f11bc15e8` becomes `contact-7c9f0fd7`).
- `normalize_entity_id(entity_id, entity_type)` -- Normalizes any entity ID variant, handling raw UUIDs, slashes in artifact paths, and different contact ID formats.
- `resolve_contact(db, name_or_ref)` -- Resolves names, `{{contact:UUID|Name}}` template references, or raw UUIDs to canonical contact IDs using database lookups.

The `reconcile` module provides `reconcile_contact_id()` which uses `ContactDirectory` to map non-canonical contact entity IDs (e.g. `contact-blair-nicol`) to canonical `contact-{hex8}` format from the contacts database.

The `contact_directory` module provides `ContactDirectory` -- an in-memory lookup loaded from the contacts database that maps contact names, UUIDs, and identifiers to canonical IDs.

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

Regenerates all memory from session history using the bulletin generator.

```bash
# Dry run -- see what would be processed without calling the LLM
cyborg memory seed --dry-run

# Full seed -- generates bulletins and runs dream pipeline
cyborg memory seed
```

The command (`seed_from_history()` in `seed.py`) follows this process:

1. Clears existing data: DELETEs from `memory_claims`, `memory_claim_bulletins`, `memory_entity_relations`, `memory_entity_bulletins`, `memory_aliases`, `memory_entities`. Sets all bulletins `digested=0`.
2. Queries all distinct session keys from `session_messages`, ordered by first message.
3. Loads known contacts from the database for entity resolution.
4. For each session with messages newer than the last bulletin:
   - Builds a transcript from messages (with sender names resolved).
   - Calls `generate_bulletins()` with the transcript and known entity hints.
   - Writes each generated bulletin to the database.
5. Runs the dream pipeline on all generated bulletins.

### cyborg memory seed-email

Regenerates memory from email thread history using the bulletin generator.

```bash
cyborg memory seed-email --dry-run
cyborg memory seed-email --thread-id <thread-id>
```

Queries `email_threads` and related tables, extracts email content, and processes through the bulletin generator. Takes an optional `--thread-id` filter.

### cyborg memory seed-manual

Replays `memory_write` tool calls from LLM logs as bulletins.

```bash
cyborg memory seed-manual --dry-run
```

Extracts `memory_write` tool calls from the `llm_call_log` table and replays them as bulletins. Deduplicates against existing `memory_bulletins` content.

### cyborg memory rebuild

Rebuilds derived data from bulletins.

```bash
# Full rebuild: clear derived tables, re-process all bulletins
cyborg memory rebuild --all
```

Clears `memory_claims`, join tables, `memory_entity_relations`, `memory_entity_bulletins`, `memory_aliases`, and `memory_entities`. Sets all bulletins `digested=0` and re-runs the dream pipeline.

### cyborg memory validate

Validates memory structure by checking that all entity documents in the database have required fields (`entity_id`, `entity_type`, `display_name`).

### cyborg memory cleanup-contacts

Merges duplicate contact entities and rewires all references to canonical IDs.

```bash
# Dry run -- show what would change without writing
cyborg memory cleanup-contacts --dry-run

# Full cleanup
cyborg memory cleanup-contacts
```

The command (`run_cleanup()` in `cleanup.py`) follows this process:

1. Loads `ContactDirectory` from the contacts database.
2. Builds a renaming map from non-canonical contact IDs to canonical `contact-{hex8}` IDs.
3. Identifies duplicate entities to merge.
4. Rewrites all references in `memory_claims`, `memory_bulletin_entities`, and `memory_entity_relations`.
5. Merges entity documents and removes duplicates.
6. Enriches contact entities with database foreign keys.

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

## Dashboard Memory Page

The dashboard includes a memory page at `/memory` with these features:

1. **Stats header** -- Total entity count and per-type counts, pulled from `GET /api/memory/stats`.
2. **Live search** -- An input field that calls `GET /api/memory/search?q=...` and displays results with abstract, relevance explanations, and latency.
3. **Pending bulletins** -- Shows all undigested bulletins with their source session and content preview.
4. **Entity browser** -- Lists entities with claim counts via `GET /api/memory/entities`, with detail views via `GET /api/memory/entities/{entity_id}`.
5. **Claims viewer** -- Lists and filters claims via `GET /api/memory/claims`.
6. **Dream log** -- A feed of dream runs showing status, bulletins consumed, claims extracted, duration, and per-bulletin operation details.
7. **Content viewer** -- An inline panel that loads any memory file's content.
8. **Validate (lint)** -- A button to trigger `POST /api/memory/lint`.
9. **Re-digest** -- Re-process a bulletin through the dream pipeline.

Dashboard API endpoints (defined in `routers/dashboard_api.py`):

| Endpoint | Description |
|----------|-------------|
| `GET /api/memory/stats` | Entity counts, per-type stats, pending bulletins, last dream time |
| `GET /api/memory/search?q=...` | Search entities, log result, return with latency |
| `GET /api/memory/searches` | Last 100 search log entries |
| `GET /api/memory/bulletins` | Pending (undigested) bulletins |
| `GET /api/memory/dreams` | Last 20 dream log entries |
| `GET /api/memory/category/{category}` | Entities in a specific type |
| `GET /api/memory/entities` | All entities with claim counts |
| `GET /api/memory/entities/{entity_id}` | Entity detail with claims |
| `GET /api/memory/claims` | List/filter claims |
| `POST /api/memory/digested` | Fetch content of specific bulletins by ID |
| `POST /api/memory/redigest` | Re-process a bulletin through dream pipeline |
| `POST /api/memory/lint` | Validate all entity documents |
| `POST /api/memory/backfill-people` | Backfill contact entities |

All endpoints are protected by the dashboard secret (Bearer token or `?secret=` query parameter).

## Key Source Files

| File | Purpose |
|------|---------|
| `services/memory/service.py` | Core service: SQLite-backed bulletin/entity CRUD, dream pipeline, search, validation |
| `services/memory/models.py` | Data models (Bulletin, Claim, EntityDocument, frontmatter parse/serialize helpers) |
| `services/memory/claim_service.py` | Claim extraction from bulletins, claim CRUD, active claim queries |
| `services/memory/entity_resolver.py` | Entity ID normalization, contact resolution |
| `services/memory/reconcile.py` | Contact ID reconciliation (non-canonical to canonical mapping) |
| `services/memory/contact_directory.py` | In-memory contacts DB lookup for name/UUID resolution |
| `services/memory/channels.py` | Session key to channel ID/visibility/scope derivation |
| `services/memory/index_service.py` | Bulk alias rebuild (`rebuild_all`) |
| `services/memory/prompts.py` | LLM system prompts for bulletin generation, claim extraction, entity update, retrieval |
| `services/memory/bulletin_generator.py` | Transcript-to-bulletin LLM pipeline with input construction and output validation |
| `services/memory/cleanup.py` | Duplicate contact entity cleanup and merging |
| `services/memory/seed.py` | Bulk history regeneration from session messages |
| `services/memory/seed_email.py` | Email history bulletin seeding |
| `services/memory/seed_manual.py` | Manual bulletin extraction from LLM call logs |
| `services/memory_tools.py` | LLM function-call tools (memory_write, memory_read, memory_search, memory_browse, memory_graph) |
| `services/prompt_assembler.py` | Injects memory tool descriptions into system prompt |
| `heartbeat.py` | SessionIdleSummaryTask triggers bulletin generation + dream after idle detection |
| `cli.py` | CLI commands: `cyborg memory seed/seed-email/seed-manual/rebuild/validate/cleanup-contacts/query` |
| `schemas/307_memory_tables.sql` | Core memory table schema (bulletins, claims, entities, relations, aliases) |
| `schemas/311_entity_bulletin_links.sql` | Join table schema for entity/claim provenance |
| `schemas/300_memory_search_log.sql` | Database schema for search logging |
| `schemas/301_memory_dream_log.sql` | Database schema for dream run logging |
| `schemas/302_memory_dream_log_raw_response.sql` | Adds raw_response column to dream log |
| `routers/dashboard_api.py` | Dashboard API endpoints for memory stats, search, bulletins, dreams, entities, claims |
| `ui_app/src/routes/memory/index.tsx` | Dashboard memory page UI component |
