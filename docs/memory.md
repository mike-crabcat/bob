# Memory System

The memory system gives Cyborg a persistent, structured knowledge base that survives across conversations. Without memory, every session starts from scratch -- the LLM has no recollection of facts, people, or preferences discussed in prior exchanges. The memory system closes this gap by recording useful information during and after conversations, then surfacing it on demand via retrieval tools.

Memory is backed by SQLite tables in the main database. A lightweight memory section describing the available tools is injected into the system prompt. When the assistant needs detail, it uses on-demand tools to read, search, or browse entries. When it learns something worth remembering, it writes a bulletin. A background dream pipeline curates bulletins into structured entity documents via an intermediate claim-extraction step.

The data model follows a four-stage pipeline:

```
channel  -->  bulletin  -->  claim  -->  entity
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
         +-----------+-----------+-----------+
         |           |           |           |
    memory_write  memory_read  recall    find
         |           |           |           |
         v           |           v           v
   +----------+      |    +------------+ +----------+
   | bulletin |      |    | Hybrid     | | Structured|
   | queue    |      |    | search     | | filter    |
   +----+-----+      |    | (embed +   | | by type   |
        |            |    |  FTS)      | +----------+
        |            |    +------+-----+
        |            |           |
        |            |     search logged to
        |            |     memory_search_log
        |            |
        |            v
        |    +------------------------------------------+
        |    | SQLite tables:                           |
        |    |   memory_bulletins                       |
        |    |   memory_claims                          |
        |    |   memory_claim_types                     |
        |    |   memory_entities                        |
        |    |   memory_entities_fts (FTS5)             |
        |    |   memory_entity_embeddings (sqlite-vec)  |
        |    |   memory_entity_bulletins                |
        |    |   memory_aliases                         |
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
    2. _ensure_entities_for_claims()  --> create entity rows
    3. _update_entity_fts()           --> update FTS + embeddings
    4. mark bulletin digested (digested=1 in DB)
```

## Data Model

Entities are the core unit. Each entity has an ID, type, display name, and a set of typed claims. Entities have no body column -- the `rendered` view is generated deterministically from claims using type-specific templates.

### Entity Types

| Type | ID format | Description |
|------|-----------|-------------|
| `person` | `person-{slug}` | A specific human being (e.g. `person-mike-cleaver`) |
| `group` | `group-{hex8}` | A WhatsApp or messaging group |
| `location` | `location-{slug}` | A place (e.g. `location-paris`) |
| `trip` | `trip-{slug}` | A trip or journey |
| `tripstop` | `tripstop-{slug}` | A stop within a trip |
| `transport` | `transport-{slug}` | A transport booking or method |
| `event` | `event-{slug}` | An event or gathering |
| `task` | `task-{slug}` | A to-do or action item |
| `file` | `file-{slug}` | A file or document (must have `file_path` claim) |
| `thing` | `thing-{slug}` | A physical object, animal, or product |
| `decision` | `decision-{slug}` | A decision that was made |

### Entity IDs

Person entities use slug-based IDs derived from names (`person-mike-cleaver`), not hex8 IDs from the contacts table. A `contact_id` claim optionally links a person entity to their contacts table row.

For all entity types, IDs use the format `{type}-{slug}` where slug is lowercase, hyphenated, alphanumeric.

## SQLite Tables

### memory_bulletins

Immutable source records. Each bulletin is a plain-text memory captured from a channel.

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PK | Auto-generated: `bulletin-YYYY-MM-DD-xxxxxx` |
| `created_at` | TEXT | ISO timestamp |
| `channel_id` | TEXT | Channel the bulletin came from |
| `source_type` | TEXT | e.g. `session`, `email`, `seed` |
| `source_id` | TEXT | Session key or thread ID |
| `visibility` | TEXT | `private`, `contact`, `group`, `channel`, `public` |
| `content` | TEXT | Plain-text bulletin body |
| `digested` | INTEGER | 0=pending, 1=processed by dream |
| `session_range_start` | TEXT | First message timestamp in this bulletin's range |
| `session_range_end` | TEXT | Last message timestamp in this bulletin's range |

### memory_claims

Atomic typed memories extracted from bulletins by the LLM. Claims are the source of truth -- entity views are derived from claims.

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PK | Auto-generated |
| `claim_type_key` | TEXT | References `memory_claim_types.key` |
| `subject_id` | TEXT | Entity ID this claim is about |
| `object_id` | TEXT | Optional target entity ID |
| `value` | TEXT | Optional text value |
| `status` | TEXT | `active`, `superseded`, `retracted`, `archived` |
| `visibility` | TEXT | Privacy level |
| `scope` | TEXT | JSON array |
| `created_at` | TEXT | ISO timestamp |
| `superseded_by` | TEXT | JSON array of claim IDs |
| `source_bulletins` | TEXT | JSON array of bulletin IDs |

Each claim has exactly one of `object_id` or `value` set (never both, never neither). `object_id` is used for entity-to-entity relationships (e.g. `spouse → person-helen-burnside`). `value` is used for text properties (e.g. `food_preference → "Thai food"`).

### memory_claim_types

Registry of valid claim type keys, applicable entity types, descriptions, and examples. Enforced by `write_claim()` -- claims with unknown keys are rejected.

Key claim types by entity:

- **person**: alias, appearance, spouse, parent, child, sibling, home_address, workplace, job, food_preference, drink_preference, interest, personality, language, birthday, contact_method, contact_id
- **group**: purpose, vibe, member
- **event**: name, start_time, end_time, location, organizer, attendee
- **trip**: destination, start_date, end_date, member, stop
- **task**: owner, due_date, description, task_status, related_entity
- **file**: name, file_path, purpose, owner
- **thing**: name, thing_type, description, owner, location
- **file_ref**: Cross-cutting -- any entity can reference a file

### memory_entities

Entity registry. No body column -- rendered views are generated from claims on demand.

| Column | Type | Description |
|--------|------|-------------|
| `entity_id` | TEXT PK | Canonical ID (e.g. `person-mike-cleaver`) |
| `entity_type` | TEXT | One of the 11 entity types |
| `display_name` | TEXT | Human-readable name |
| `status` | TEXT | `active`, `archived` |
| `created_at` | TEXT | ISO timestamp |
| `updated_at` | TEXT | ISO timestamp |

### memory_entities_fts

FTS5 virtual table for keyword search. Indexed content is the rendered entity body (same text shown in the UI).

### memory_entity_embeddings

sqlite-vec virtual table for semantic search. Stores 1536-dimension embeddings from OpenAI's `text-embedding-3-small` model.

### memory_aliases

Display-name to entity-ID lookup.

| Column | Type | Description |
|--------|------|-------------|
| `alias` | TEXT PK | Display name |
| `entity_id` | TEXT FK | Referenced entity |

### memory_entity_bulletins

Many-to-many link between entities and their source bulletins for provenance.

## Rendering

`render_entity(entity_type, display_name, claims)` in `claim_types.py` generates a human-readable text block from an entity's claims using type-specific templates. Each entity type has an ordered list of (claim_type_key, label) pairs. The renderer groups claims by type, applies labels, and formats as indented bullet lists.

This rendered text is used for:
- FTS5 indexing (stored in `memory_entities_fts`)
- Embedding generation (stored in `memory_entity_embeddings`)
- Dashboard UI display
- Agent tool responses

## Retrieval

### recall(query)

The primary retrieval tool. Resolves a query to entity/claims in this order:

1. **Exact entity ID** -- direct lookup in `memory_entities`
2. **Alias lookup** -- case-insensitive match in `memory_aliases`
3. **Embedding search** -- semantic similarity via `memory_entity_embeddings` (returns top 5, rendered)
4. **FTS5 fallback** -- keyword AND-search across rendered bodies

Embedding search uses cosine distance via sqlite-vec with a threshold of 1.2. When multiple close matches are found, the top result is rendered in full with additional matches appended. This enables queries like "what type of car does david have" to find `thing-subaru` via semantic similarity, even though neither "david" nor "car" appear in the Subaru entity's rendered text.

### find(entity_type, claim_type_key?, value?)

Structured search -- list entities of a given type, optionally filtered by claim type and value.

### note(text)

Queue a manual bulletin for dream processing.

## The Dream Process

The dream process transforms raw bulletins into structured entities. It runs automatically after each heartbeat cycle. Per bulletin:

1. **Claim extraction**: `extract_claims_from_bulletin()` sends the bulletin to an LLM with the claim type registry. The LLM returns a JSON array of claim objects. Claims are validated against `memory_claim_types` and normalized (entity ID slugs, new-person resolution).

2. **Entity creation**: `_ensure_entities_for_claims()` creates `memory_entities` rows for any new entity IDs referenced in claims.

3. **Index maintenance**: `_update_entity_fts()` renders the entity via template, updates the FTS5 index, and generates an embedding vector for semantic search.

4. **Digestion**: Bulletin is marked `digested=1`.

Bulletin content is pre-mapped before extraction: `{{contact:HEX8|Name}}` tags are replaced with `{{person-slug|Name}}` so the LLM sees person-entity IDs, not raw contact UUIDs.

## Contact Integration

Each contact in the `contacts` table can have an associated `person` entity. The link is bidirectional:

- **Contact → Person**: Dashboard API `/api/contacts/{id}/entity` finds the person entity by `contact_id` claim, falling back to name-slug matching.
- **Person → Contact**: The `contact_id` claim stores the hex8 ID for contacts table lookup.
- **Contact detail page**: Shows the person entity's rendered content and a "view in memory" link.

## Bulletin Detail Pages

The dashboard has dedicated bulletin detail pages at `/memory/bulletins/{bulletinId}` showing:
- Source session/type, channel, visibility
- Full bulletin text
- All claims extracted from this bulletin (with links to their entities)

## Dashboard Memory Page

The dashboard includes a memory page at `/memory` with these tabs:

### Entities Tab
- Entity list with claim counts, filterable by type
- Entity detail view with rendered body, claims (with directional arrows showing subject/object), and source bulletins
- Bulletin links navigate to bulletin detail pages

### Pipeline Tab
- Pending (undigested) bulletins with source and content preview
- Dream run log with per-bulletin breakdown showing claims extracted and entity operations
- Bulletin slugs link to bulletin detail pages

### Search Tab
- Live search input using `GET /api/memory/search?q=...`
- Hybrid search: FTS5 keyword search first, then embedding similarity fallback
- Search history from `memory_search_log`

### Dashboard API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/memory/stats` | Entity counts, per-type stats, pending bulletins |
| `GET /api/memory/search?q=...` | Search entities (FTS + embedding fallback) |
| `GET /api/memory/bulletins` | Pending (undigested) bulletins |
| `GET /api/memory/bulletins/{id}` | Bulletin detail with claims |
| `GET /api/memory/dreams` | Last 20 dream log entries |
| `GET /api/memory/entities` | All entities with claim counts |
| `GET /api/memory/entities/{entity_id}` | Entity detail with rendered body and claims |
| `GET /api/contacts/{id}/entity` | Person entity for a contact (with rendered body) |
| `GET /api/contacts/{id}/claims` | Claims for a contact's person entity |

## CLI Commands

### cyborg memory rebuild

```bash
# Full rebuild: clear derived tables, re-process all bulletins
cyborg memory rebuild --all

# Rebuild a single entity
cyborg memory rebuild --entity person-mike-cleaver
```

Clears `memory_claims`, `memory_entity_bulletins`, `memory_entity_relations`, `memory_aliases`, `memory_entities_fts`, `memory_entity_embeddings`, `memory_entities`. Sets all bulletins `digested=0` and re-runs the dream pipeline.

### cyborg memory cleanup-contacts

Merges duplicate person entities and rewires all references to canonical IDs.

### cyborg memory query

Queries memory with a natural language question via the `recall()` tool.

## Embedding Search Details

- **Model**: OpenAI `text-embedding-3-small` (1536 dimensions)
- **Storage**: sqlite-vec `vec0` virtual table, embeddings stored as packed float32 blobs
- **Indexing**: Embeddings are generated when entities are created/updated (inside `_update_entity_fts`)
- **Rebuild**: `svc.rebuild_embeddings()` batch-embeds all entities (up to 100 per OpenAI API call)
- **Query**: Embed the search query, then cosine-distance search against all entity embeddings
- **Cost**: ~$0.02 per 1M tokens. With ~100 entities at 1-2KB each, a full rebuild costs under $0.01

## Key Source Files

| File | Purpose |
|------|---------|
| `services/memory/service.py` | Core service: CRUD, dream pipeline, search, FTS/embedding indexing |
| `services/memory/models.py` | Data models (Bulletin, Claim) and entity type registry |
| `services/memory/claim_service.py` | Claim extraction from bulletins, claim CRUD, entity ID normalization |
| `services/memory/claim_types.py` | Claim type registry, entity templates, `render_entity()` |
| `services/memory/embedding.py` | OpenAI embedding API, sqlite-vec similarity search |
| `services/memory/tools.py` | Agent tools: `recall()`, `find()`, `note()` |
| `services/memory/prompts.py` | LLM system prompts for bulletin generation and claim extraction |
| `services/memory/entity_resolver.py` | Entity ID normalization |
| `services/memory/contact_directory.py` | In-memory contacts DB lookup |
| `services/memory/cleanup.py` | Duplicate person entity cleanup |
| `services/memory/bulletin_generator.py` | Transcript-to-bulletin LLM pipeline |
| `database.py` | SQLite connection pool with sqlite-vec extension loading |
| `routers/dashboard_api.py` | Dashboard API endpoints |
| `ui_app/src/routes/memory/index.tsx` | Memory page UI (entities, pipeline, search tabs) |
| `ui_app/src/routes/memory/bulletins/$bulletinId.tsx` | Bulletin detail page |
| `ui_app/src/routes/contacts/$contactId.tsx` | Contact detail with person entity section |
