# Memory System

Bob's memory system is a persistent, structured knowledge base that survives across conversations. Without it, every session would start from scratch: the LLM would have no recollection of people mentioned, plans made, preferences expressed, or facts established in earlier exchanges. The memory system closes that gap by recording information during and after conversations, then surfacing it on demand through retrieval tools.

Memory is backed by SQLite tables in the main database. A short "Memory" section listing the available tools is injected into every system prompt by `services/prompt_assembler.py`. When the agent needs detail it does not already have, it calls `recall` to look up an entity by name or question, `find` to do structured filtering by type and claim, or `note` / `memory_write` to capture something new. A background pipeline curates those raw captures into typed, atomic claims and reconciles them against per-type consistency rules.

## Why memory exists, and the design choices it forces

The fundamental problem Bob solves is continuity: facts that span many sessions, channels, and weeks need to live somewhere the LLM can reach without being re-told each time. Two design decisions dominate the architecture, and both are deliberate tradeoffs.

**Claim-centric storage instead of free-text documents.** The system does not store "Mike's profile" as a paragraph of prose. It stores a set of atomic, typed claims — `food_preference: "loves Thai food"`, `spouse: person-helen-burnside`, `home_address: "42 Bondi Rd, Sydney"` — and renders them into prose on demand via per-entity-type templates. The cost is rigidity: every claim type has to be declared in the registry, and the LLM has to play along. The benefit is enormous: claims can be deduplicated, superseded, retracted, queried by type, scoped per channel, traced to their source bulletins, and rendered deterministically. Free-text documents give you none of that. The `claim_types.py` registry and the `entity_templates/` Jinja2 templates together define the contract.

**Extraction-on-write instead of retrieval-time reasoning.** The expensive reasoning — what facts exist in this conversation, what entity each one is about, whether it conflicts with something already known — happens once, in the background, when a transcript is fresh and the context is small. Retrieval at runtime is then cheap: a handful of SQL queries plus an embedding lookup. The alternative (dump the raw transcript into the prompt every time the agent needs a fact) does not scale and gives the LLM a much larger, noisier context to reason over. The cost of extraction-on-write is occasional extraction errors and a need for downstream reconciliation; the curation pipeline below is what handles both.

## Architecture overview

The data flows in four stages, from raw input through to a rendered entity view:

```
   channel ──►  bulletin  ──►  claim  ──►  entity
   (source)     (record)     (atom)     (derived view)
   ────────     ────────     ──────     ─────────────
   live note    immutable    typed      identity row
   transcript   plain text   fact       + rendered text
   email body   captures     about an   generated from
   manual seed  the event    entity     claims via
                            (source of  Jinja2 template
                             truth)
```

A bulletin is the immutable record of something that happened. Claims are the atomic facts the dream pipeline extracts from bulletins. An entity is a small identity row plus a rendered view that the templates regenerate from its active claims. Bulletins are never mutated; claims can be superseded or retracted; entities are derived and rebuildable.

The pipeline that turns sessions into bulletins and bulletins into clean entity state runs across two loops. The first is a synchronous loop, triggered by every bulletin write, that extracts claims and updates entity views. The second is a debounced loop that runs supplement and reconciliation on the entities that were just touched.

```
                       ┌──────────────────────────────────────┐
                       │           Sessions / inputs          │
                       │  (WhatsApp, email, voice, manual)    │
                       └────────────────────┬─────────────────┘
                                            │
                  ┌─────────────────────────┴───────────────────────┐
                  │                                                 │
            live writes                                      idle sessions
            (note, memory_write,                 (SessionIdleSummaryTask in
             /bulletin command)                   heartbeat.py, every 5 min)
                  │                                                 │
                  │                       generate_session_bulletins()
                  │                                  writes raw-transcript
                  │                                  bulletin with prior-context
                  │                                  header + [SYNTHETIC] tags
                  │                                                 │
                  └─────────────────────────┬───────────────────────┘
                                            │
                                            ▼
                              ┌──────────────────────────┐
                              │    memory_bulletins      │  (immutable)
                              │  (raw_transcript or      │
                              │   llm_summary format)    │
                              └────────────┬─────────────┘
                                   write_bulletin()
                                   debounced _schedule_dream()
                                            │
                                            ▼
              ┌──────────────────────────────────────────────────┐
              │           run_dream()  →  process_bulletin()     │
              │                                                  │
              │  extract_claims_from_bulletin() via LLM          │
              │    - skips [SYNTHETIC] lines + prior-context     │
              │    - validates claim_type_key against registry   │
              │    - normalises entity IDs, resolves person:new  │
              │  write_claim() deduplicates by content          │
              │  _ensure_entities_for_claims() creates rows     │
              │  _update_entity_fts() renders + indexes         │
              └─────────────────────┬────────────────────────────┘
                                    │
            digested=1 ──────►  memory_bulletins
                                    │
                       _schedule_reconciliation(touched)
                                    │ (2s debounce)
                                    ▼
              ┌──────────────────────────────────────────────────┐
              │  Phase 0: deprecate_file_entities_without_path() │
              │  Phase 1: supplement_entity()                    │
              │    - collects bulletins from entity + refs       │
              │    - LLM gap-fills missing claims                │
              │    - allows inference from related data          │
              │    - skips entity-ref claims (no inferred links) │
              │  Phase 2: reconcile_entity()                     │
              │    - LLM with tools (list_entities, get_entity,  │
              │      add_claim, retract_claim, supersede_claim,  │
              │      create_entity, delete_entity, merge_entities)│
              │    - checks per-type reconciliation_rules        │
              │    - applies fixes directly, raises questions    │
              │      for genuinely ambiguous cases               │
              └──────────────────────────────────────────────────┘
```

## Data model

### Bulletins — the immutable record

A bulletin is a plain-text capture of something memory-worthy, stored once and never edited. Bulletins are the system's ground truth; everything else is derived from them and rebuildable.

`memory_bulletins` (schema `307`, simplified in `310`, extended in `312` and `338`):

| Column | Description |
|--------|-------------|
| `id` | `bulletin-YYYY-MM-DD-xxxxxx`, generated on write |
| `created_at` | ISO timestamp of the bulletin (or `occurred_at` override) |
| `channel_id` | Canonical channel ID derived from the session key by `channels.resolve_channel_id` |
| `source_type` | `session`, `email`, `manual`, `note`, `seed`, etc. |
| `source_id` | Session key, thread ID, or empty |
| `visibility` | `private`, `contact`, `group`, `channel`, `public` |
| `content` | Plain-text body |
| `digested` | 0 = pending, 1 = processed by dream |
| `session_range_start`, `session_range_end` | Timestamp window the bulletin covers (used to find the next idle window) |
| `format` | `raw_transcript` (current) or `llm_summary` (legacy) |

There are two formats because the system migrated from LLM-summarised bulletins to literal transcripts. Legacy bulletins contain plain prose; raw-transcript bulletins contain a literal window of session messages prefixed by a prior-context block. Both are handled by the extraction prompt.

### Claims — the atomic fact

A claim is a single typed proposition about one entity. Each claim has exactly one `subject_id` (the entity it is about) and exactly one of `object_id` (a reference to another entity) or `value` (a scalar like a date or a string). The `claim_type_key` is validated against `memory_claim_types`. Claims are the source of truth for everything the system knows.

`memory_claims` (schema `307`, rewritten as v2 in `318`):

| Column | Description |
|--------|-------------|
| `id` | Generated: `claim-{bulletin_id}-{n}` for extraction, `claim-recon-{hex8}` for reconciliation, `claim-suppl-{hex8}` for supplement |
| `claim_type_key` | FK to `memory_claim_types.key`; unknown keys are rejected |
| `subject_id` | Entity ID this claim is about |
| `object_id` | Entity reference (mutually exclusive with `value`) |
| `value` | Scalar value (mutually exclusive with `object_id`) |
| `status` | `active`, `superseded`, `retracted`, `expired`, `disputed`, `archived`, `redundant`, `disproven`, `obsolete` |
| `visibility` | `private`, `contact`, `group`, `channel`, `public` |
| `scope` | JSON array of scope tags |
| `source_bulletins` | JSON array of bulletin IDs — the provenance trail |
| `superseded_by` | JSON array of claim IDs or labels (`"reconciliation"`) that superseded this one |
| `created_at` | ISO timestamp |

Claims deduplicate on write: if an active claim with the same `(claim_type_key, subject_id, object_id|value)` already exists, `write_claim` merges bulletin sources into the existing row instead of inserting a duplicate. This is what lets the same fact being re-extracted from multiple bulletins collapse into one claim with multiple sources.

### Claim types — the contract

`memory_claim_types` (schema `317`, evolved by `321` and `334`) is the registry of valid keys. Each row records the `key`, which entity types it applies to, a description, and an example. The registry is also mirrored in code at `claim_types.py:CLAIM_TYPE_REGISTRY` so the extraction prompt can be built from Python dataclasses and so per-type metadata (keywords, reconciliation rules, rendering templates) lives in one place. Adding a new claim type requires touching both; the code is the source of truth for behaviour, the SQL table is the runtime validation gate.

Claim types are grouped by what they express:

- **Person facts**: `alias`, `appearance`, `spouse`, `parent`, `child`, `sibling`, `grandparent`, `grandchild`, `home_address`, `workplace`, `job`, `food_preference`, `drink_preference`, `dietary_restriction`, `music_preference`, `sport_preference`, `entertainment_preference`, `pet`, `interest`, `personality`, `language`, `birthday`, `contact_method`, `hometown`, `contact_id`, `communication_style`, `preference`
- **Group / event / location**: `purpose`, `vibe`, `member`, `name`, `start_time`, `end_time`, `location`, `organizer`, `attendee`, `recurrence`, `associated_trip`, `location_type`, `parent_location`, `address`, `associated_contact`, `opening_hours`
- **Trip composition**: `leg` (a stay), `attraction`, `dayplan`, `connection` — these point at child entities
- **Stay** (accommodation leg): `accommodation`, `accommodation_type`, `accommodation_address`, `arrival_date`, `departure_date`
- **Connection** (transport hop): `departure_location`, `arrival_location`, `departure_time`, `arrival_time`, `transport_type`, `duration`, `booking_ref`, `route`, `passenger`, `seat`
- **Attraction / dayplan**: `attraction_type`, `visit_date`, `cost`, `date`, `notes`
- **Task / file / thing / decision**: `owner`, `due_date`, `description`, `task_status`, `related_entity`, `file_path`, `thing_type`, `decider`, `rationale`
- **Cross-cutting**: `file_ref` (any entity → file), `truth` (user-stated corrections that override inference)

### Entities — identity plus a derived view

`memory_entities` (schema `307`, evolved through `319`, `321`, `325`) holds only identity: `entity_id`, `entity_type`, `display_name`, `status`, `created_at`, `updated_at`. There is no body column. The rendered text the agent sees is generated on demand by `claim_types.render_entity()`, which loads the entity's active claims and feeds them through a Jinja2 template.

Entity IDs follow `{type}-{slug}`. Person IDs use name slugs (`person-mike-cleaver`), not contact UUIDs, because humans are addressed by name and the slug is stable across contact-record churn. The link to the contacts table is the `contact_id` claim, whose value is the hex8 ID. Group entities are created with random hex8 suffixes (`group-{hex8}`) and back-referenced from `whatsappgroups.memory_entity_id`. File entities are special: they are only created if the source material provides a valid `file_path`, and entities that lose their path are deprecated by `deprecate_file_entities_without_path()`.

The current entity types, defined in `ENTITY_TYPE_REGISTRY`, are: `person`, `group`, `location`, `trip`, `stay`, `attraction`, `dayplan`, `connection`, `event`, `task`, `file`, `thing`, `decision`. Each entry in the registry carries keywords for text-based detection, extraction rules injected into the prompt, reconciliation rules consumed by the recon agent, and flags like `skip_expand` (don't recurse into this type during recon) and `follow_for_bulletins` (walk this ref when collecting related bulletins for supplement).

### Rendering — from claims to text

`render_entity()` in `claim_types.py` is what turns the claim set into the prose the agent and dashboard see. For entity types with a Jinja2 template in `services/memory/entity_templates/` (`trip.md`, `stay.md`, `connection.md`), the renderer loads the template, groups claims by type, recursively resolves entity references via the database, and renders. For all other types it falls back to `_render_entity_generic`, which uses the in-code `_ENTITY_TEMPLATES` dict (an ordered list of `(claim_type_key, label)` pairs per type) to lay out claims as labelled lines and bullet lists. Either way, the rendered text is deterministic given the same claim set — there is no LLM in the rendering path.

The same rendered text is used for FTS indexing, embedding generation, dashboard display, and tool responses, so an entity's appearance is consistent everywhere.

### Indexes and supporting tables

- `memory_entities_fts` (`314`, recreated by `320`) — FTS5 virtual table over `(entity_id, display_name, rendered_body)`. The renderer's output is the indexed content.
- `memory_entity_embeddings` (`322`) — sqlite-vec `vec0` virtual table mapping `entity_id` to a 1536-dimensional `text-embedding-3-small` vector of the rendered body.
- `memory_aliases` (`307`) — display name → entity ID lookup, populated on entity write.
- `memory_entity_bulletins` (`311`) — many-to-many link between entities and their source bulletins, used for provenance and rebuild.
- `memory_claim_bulletins` (`311`) — many-to-many link between claims and bulletins.
- `memory_questions` (`323`) — reconciliation questions awaiting a human answer.
- `memory_search_log` (`300`) — recent dashboard searches.
- `memory_dream_log` (`301`, extended by `302`) — per-run audit trail of the dream pipeline.
- `recon_model_overrides` (`337`) — per-entity reconciliation model overrides.

## Authoring paths — how facts enter memory

There are two distinct ways a fact becomes a bulletin. Both end up in the same `memory_bulletins` table; the difference is who triggers the write.

### Live writes during a conversation

When the agent decides something is worth remembering mid-conversation, it calls one of two tools registered by `services/memory_tools.py:make_memory_tools()`:

- **`note(text, context_entity_id?)`** — the lightweight entry point. The tool inserts a row directly into `memory_bulletins` with `source_type="note"`, derived channel ID, and visibility derived from the session kind. Optionally links the bulletin to a context entity.
- **`memory_write(content, channel_id?, visibility?)`** — the explicit form. Calls `MemoryService.write_bulletin()` directly, which is the same path the background pipeline uses. Returns the bulletin ID.

Both tools route through `write_bulletin()`, which inserts the row and then calls `_schedule_dream()`. The dream is debounced: if another bulletin arrives within 2 seconds, the timer resets. This collapses a flurry of writes (e.g. the agent capturing several facts from one user turn) into a single extraction pass.

### Background bulletin generation from idle sessions

The other authoring path does not require the agent to do anything. `SessionIdleSummaryTask` in `heartbeat.py` runs every heartbeat cycle (default 60s) and looks for sessions that have been idle longer than `session_summary_idle_minutes` (default 5). For each, it calls `MemoryService.generate_session_bulletins()`.

`generate_session_bulletins()` is the bridge from raw chat history to a bulletin. It:

1. Queries `memory_bulletins` for the most recent `session_range_end` for this session — this is the upper bound of the last bulletin's coverage. Everything after that is "new".
2. Queries `session_messages` for the window between that bound and the most recent message. These are the messages that will be extracted.
3. Fetches the prior N messages (configurable via `bulletin_prior_context_messages`, default 5) and includes them under a header line `Prior messages (context only, do not extract):`. This gives the LLM continuity so it can resolve pronouns and references without re-extracting facts that were already captured from the previous window.
4. Formats each message as `[<iso_ts>] [<name> <contact_id>][SYNTHETIC]: <content>`. The `[SYNTHETIC]` tag is set when the assistant message was generated during a dispatch that used a memory-read tool — see below.
5. Writes a single bulletin with `format="raw_transcript"`, the window timestamps in `session_range_start` / `session_range_end`, and visibility derived from the session kind.
6. Calls `ensure_group_entity()` if the session is a group chat, making sure the group has an entity row and is linked to this bulletin.

The same `generate_session_bulletins()` is also called by the WhatsApp bridge when an operator sends `/bulletin` in a chat — that path forces immediate bulletin generation rather than waiting for idle.

### How prior-context windows and synthetic-echo flagging prevent re-ingestion

The two hardest problems in extraction-on-write are (a) re-extracting the same fact from overlapping windows and (b) treating the agent's own recollections as new ground truth. Both are addressed structurally.

**Window boundaries.** Each bulletin records its `session_range_end`. The next call to `generate_session_bulletins()` uses `MAX(session_range_end)` as the lower bound, so messages are never in two bulletins' "Window messages" sections. They may appear in the "Prior messages (context only, do not extract):" header of the next bulletin, but the extraction prompt explicitly tells the LLM to skip that section.

**Synthetic-echo flagging.** When the agent calls `recall`, `find`, or `memory_read` during a dispatch, the resulting assistant message is an echo of existing memory — not new ground truth. `LLMDispatchService` tracks this: a callback in `_make_tool_callback` flips `_memory_tool_used[dispatch_id] = True` whenever one of the memory-read tools fires. When `SessionService.add_message()` stores the assistant response, it calls `LLMDispatchService.pop_memory_used(dispatch_id)` and writes `synthetic=1` into `session_messages` if the flag was set. The extraction prompt's first rule is then: "skip any line tagged `[SYNTHETIC]`."

Together, these mean a fact gets extracted exactly once: the first time it appears in a window's non-synthetic messages. Subsequent appearances are either in a prior-context header (skipped by rule) or in a synthetic-tagged assistant turn (skipped by rule).

## The curation pipeline

Raw bulletins become clean entity state through three stages, each with a distinct responsibility and a distinct relationship to the LLM.

### Stage 1 — claim extraction (dream)

`run_dream()` is the entry point. It reads all `digested=0` bulletins in chronological order and calls `process_bulletin()` on each. `process_bulletin()`:

1. Resolves the group entity ID if the bulletin came from a group session, so the LLM can attribute group-level claims correctly.
2. Loads the contact directory and formats it as a roster mapping `contact-{hex8}` to `person-{slug}`. This is critical: bulletins reference people by contact ID (because that's what message attribution uses), but person entities are slug-based.
3. Pre-maps `{{contact:HEX8|Name}}` tags in the bulletin content to `{{person-slug|Name}}` tags, so the LLM sees the canonical person entity IDs inline.
4. Detects entity types mentioned in the text using the registry's keyword lists, and builds an extraction prompt section that includes only the relevant claim types. This keeps the prompt small and the LLM focused.
5. Calls `extract_claims_from_bulletin()`, which sends the bulletin to the LLM with `call_category="memory_claim_extraction"` and `model=llm.memory_model`. The prompt is `prompts._CLAIM_EXTRACTION_TEMPLATE` populated with the claim-type glossary.
6. The LLM returns a JSON array of claim objects. The code validates each one: rejects unknown `claim_type_key`s, normalises entity IDs (`file:foo` → `file-foo`, double prefixes stripped, `person:new:Name` resolved to `person-{slug}`), drops file entities without a valid `file_path`, and enforces the exactly-one-of `object_id`/`value` constraint.
7. Each surviving claim is written via `write_claim()`, which deduplicates against existing active claims.
8. `_ensure_entities_for_claims()` creates `memory_entities` rows for any new entity IDs, resolving display names from contact-id claims or slug capitalisation.
9. `_update_entity_fts()` renders each touched entity, updates the FTS row, and upserts the embedding.
10. The bulletin is marked `digested=1`.

After all bulletins are processed, the set of touched entity IDs is collected and passed to `_schedule_reconciliation()`.

### Stage 2 — supplement (gap-filling)

`supplement_entity()` runs after extraction, before reconciliation. Its job is to fill in claims that the strict extraction pass missed but that can be inferred from related data. The key difference from extraction is that supplement is allowed to infer: a transport booking implies a stay's departure date, a hotel check-in implies a stay's arrival date, a sibling stay's address implies a parent trip's destination region. Extraction is not allowed to do this — it must only record what the bulletin literally says.

The supplement pass:

1. Collects bulletins from the entity itself, plus from related entities found by walking `ENTITY_REF_CLAIM_KEYS` (parent trip, sibling stays, linked transports/locations, etc.) for two hops. This is the `follow_for_bulletins` flag on the entity type registry at work.
2. Loads the entity's current active claims as a dedup set.
3. Calls the LLM with `call_category="memory_supplement"` and a prompt that explicitly says "you MAY infer entity claims from related information" and "every claim must be a fact ABOUT the target entity."
4. For each returned claim, skips entity-ref claims (those must come from explicit extraction, not inference), skips claims already present unless they upgrade a placeholder value containing `??`, and writes the rest via `write_claim()` with `source_bulletins` set to the scanned set.

Supplement is non-destructive: existing claims are never modified or removed by this stage.

### Stage 3 — reconciliation (consistency)

`reconcile_entity()` is the most powerful stage and the only one with write tools. Where extraction and supplement are "extract-then-write-JSON" calls, reconciliation is a tool-using agent loop. The LLM is given:

- The entity rendered via `render_entity_full()` — a recursive view that expands entity-ref claims one level deep, with provenance tags on every claim (which bulletins it came from, or `[source: none — inferred]` for claims with no bulletin).
- The source bulletin text for the entity's claims, collected by `_collect_bulletin_text()`.
- Any previously-answered questions for this entity, treated as ground truth.
- The per-type `reconciliation_rules` from the entity type registry — concrete rules like "Stay date ranges must not overlap", "Each distinct accommodation MUST be its own stay entity", "Connection claims should reference connection entities whose departure_time falls within the trip's overall date range".

And a set of tools, built by `make_reconciliation_tools()`:

- `list_entities(entity_type)` — discover related entities.
- `get_entity(entity_id)` — full rendered view with provenance and reverse references.
- `add_claim(subject_id, claim_type_key, value?, object_id?)` — write a new claim.
- `retract_claim(subject_id, claim_type_key, old_value?)` — supersede an active claim.
- `supersede_claim_tool(subject_id, claim_type_key, old_value, new_value?, new_object_id?)` — replace a claim.
- `create_entity(entity_id, entity_type, claims_json?)` — new entity with initial claims.
- `delete_entity(entity_id)` — archive entity and supersede its claims.
- `merge_entities(canonical_id, loser_id)` — absorb one entity into another, rewriting all references.

The LLM is instructed to "prefer acting over asking" — it should apply fixes directly via tools. Only when a fix is genuinely ambiguous (e.g. two overlapping stays where the user might have intended two hotels) does it raise a question, which lands in `memory_questions` for the operator to answer. Answered questions become `truth` claims and are queued for re-reconciliation.

The model used for reconciliation is configurable. `resolve_reconciliation_model()` checks, in order: a per-entity override in `recon_model_overrides`; whether the entity type is in `settings.reconciliation.large_model_types` (which routes to `openai.default_model`); otherwise falls back to `openai.memory_model`. This lets the operator spend more on reconciling complex trip itineraries while keeping simple person-entity reconciliation cheap.

## Retrieval — how the agent accesses memory

The agent sees memory through three tools registered by `services/memory_tools.py`. The same primitives back the dashboard search.

### `recall(query)`

The primary retrieval tool. `recall()` in `services/memory/tools.py` resolves the query to an entity through a four-step cascade:

1. **Exact entity ID** — direct lookup in `memory_entities`. `recall("person-mike-cleaver")` lands here.
2. **Alias lookup** — case-insensitive match in `memory_aliases` against the query. `recall("Mike")` lands here if "Mike" is an alias.
3. **Embedding search** — `search_similar()` in `embedding.py` embeds the query via `text-embedding-3-small`, packs the vector as float32 bytes, and runs a cosine-distance search against `memory_entity_embeddings` with `distance < 1.2`, returning the top 5. The closest match becomes the primary result; the remaining matches are rendered and appended below a `---` separator. This is what makes `recall("what kind of car does david have")` find `thing-subaru` even though neither "david" nor "car" appears in the Subaru entity's rendered text.
4. **FTS5 fallback** — if embedding search returns nothing, the query is tokenised into quoted terms joined by `AND` and matched against `memory_entities_fts`.

Once an entity is resolved, `recall` loads its active claims, renders them via `render_entity()`, appends a "Referenced by:" section listing reverse references (claims where this entity is the `object_id`), and appends any extra embedding matches. The agent receives one text block per call.

### `find(entity_type, claim_type_key?, value?)`

Structured search. Filters entities by type and optionally by claim type and value. With no filters, lists all active entities of the type. Used for queries like "list all trips" or "find all tasks with status open".

### `note(text, context_entity_id?)` and `memory_write(content, channel_id?, visibility?)`

The write side — see "Authoring paths" above.

### `memory_correct(action, entity_id?, ...)`

A correction tool for fixing wrong memory. Actions are `remove_entity` (archive entity + supersede all claims + write a `truth` claim to prevent re-creation), `remove_claim` (supersede a specific claim), and `set_truth` (write a user-stated correction). Always requires a reason. This is the agent-facing equivalent of the reconciliation tools, scoped to corrections only.

### How retrieval results are injected

Retrieval results come back as plain text in the tool response. The agent incorporates them into its reasoning and produces its reply. There is no separate "memory context" injected into the system prompt at runtime — the system prompt only contains the tool list and a one-line index. The agent pulls what it needs on demand. This keeps the prompt small and avoids stale context.

## Visibility and scope

Every bulletin and every claim carries a `visibility` field with one of five values: `private`, `contact`, `group`, `channel`, `public`. The default is derived from the session kind by `channels.derive_visibility()`: group sessions produce `group`-visibility bulletins, DMs produce `contact`, everything else defaults to `private`.

Visibility is propagated from bulletin to claim during extraction — each claim inherits its source bulletin's visibility. The `scope` field on claims is a JSON array of finer-grained tags (e.g. `["public", "group-12036342829458"]`) derived from the session key by `derive_scope()`.

The visibility boundary is enforced at the claim level. When the agent renders an entity, it sees all active claims regardless of visibility (the current implementation does not filter retrieval by caller context); the visibility tag is the contract for future enforcement and for the operator to understand who should see what. The `QueryContext` model in `models.py` carries `actor`, `channel_id`, and `allowed_scopes` for callers that want to enforce filtering.

## Operability

The system is designed to be inspectable and repairable without a full rebuild. Tooling lives in the CLI (`packages/bob-server/bob_server/cli.py`, exposed as `bob memory ...` via Typer), the dashboard API (`routers/dashboard_api.py`), and the UI (`ui_app/src/routes/memory/`).

### CLI subcommands

All commands are under `bob memory` (the Typer subapp `memory_app`):

- **`bob memory rebuild [--all | --entity ID]`** — full or per-entity rebuild. `--all` clears `memory_claims`, the join tables, aliases, FTS, embeddings, and entities; resets all bulletins to `digested=0`; re-runs the dream pipeline; rebuilds embeddings; and reconciles all trips. `--entity` reprocesses only the bulletins linked to one entity.
- **`bob memory reconcile [IDs...] [--all] [--render]`** — run reconciliation. `--render` dumps the recursive entity view without calling the LLM, useful for debugging what the recon agent would see.
- **`bob memory supplement IDs...`** — gap-fill specific entities.
- **`bob memory merge [--dry-run]`** — detect duplicate entities via embedding similarity and merge them.
- **`bob memory reindex`** — rebuild the FTS index only, no LLM calls.
- **`bob memory validate`** — structural checks (missing display names, etc.).
- **`bob memory cleanup-contacts [--dry-run]`** — merge duplicate person entities and rewire references.
- **`bob memory query QUESTION [--type T]`** — natural-language search via the same path as the dashboard.
- **`bob memory seed [--dry-run]`**, **`seed-email [--thread ID]`**, **`seed-manual`** — regenerate bulletins from session history, email threads, or replayed `memory_write` tool calls in LLM logs.
- **`bob memory model-override-set ENTITY_ID MODEL [--reason ...]`**, **`model-override-remove ENTITY_ID`**, **`model-override-list`** — manage per-entity reconciliation model overrides in `recon_model_overrides`.

### Dashboard API endpoints

All under `/api/memory/...` in `routers/dashboard_api.py`, authenticated by the dashboard secret:

- `GET /api/memory/stats` — entity counts by type, recent entries, pending bulletin count, last dream timestamp.
- `GET /api/memory/search?q=...` — hybrid FTS-then-embedding search; logged to `memory_search_log`.
- `GET /api/memory/searches` — recent search history.
- `GET /api/memory/bulletins` — pending (undigested) bulletins.
- `GET /api/memory/bulletins/{id}` — bulletin detail with all claims extracted from it.
- `GET /api/memory/dreams` — last 20 dream log entries with per-bulletin breakdown.
- `GET /api/memory/entities?type=...` — all entities with claim counts and per-type summary fields.
- `GET /api/memory/entities/{id}` — entity detail with rendered body, claims (with `subject → object` directionality), and source bulletins.
- `GET /api/memory/claims?type=...&subject_id=...&status=...` — paginated claim browser.
- `GET /api/memory/questions?status=open` — reconciliation questions awaiting an answer.
- `POST /api/memory/questions/{id}/answer` — answer a question (writes a `truth` claim, queues re-reconciliation).
- `POST /api/memory/questions/{id}/dismiss` — dismiss without answering.
- `GET /api/contacts/{id}/entity` — person entity for a contact, located by `contact_id` claim or name-slug fallback.
- `GET /api/contacts/{id}/claims` — claims for a contact's person entity.

### Dashboard pages

The React UI at `/memory` exposes tabs for entities (list + detail with rendered body, claims, and bulletin links), the pipeline (pending bulletins, dream log), search (live hybrid search with history), and reconciliation questions. Bulletin slugs throughout link to `/memory/bulletins/{id}` detail pages showing the raw bulletin text alongside every claim extracted from it. Contact detail pages embed a "person entity" section showing the rendered body and a link into memory.

### Per-entity model overrides

Some entities are harder to reconcile than others. A complex multi-leg trip with merged stays and overlapping connections may need a larger model than a simple person entity. The `recon_model_overrides` table (schema `337`) lets the operator pin a specific model to a specific entity ID. Resolution happens in `resolve_reconciliation_model()`: per-entity override wins; otherwise per-type config (`settings.reconciliation.large_model_types`) routes to the large model; otherwise the small memory model is used. The CLI subcommands `model-override-set`, `model-override-remove`, `model-override-list` manage the table directly.

## Integration points

Memory plugs into the rest of Bob at three places: prompt assembly, tool registration, and LLM dispatch.

```
                    ┌─────────────────────────────────┐
                    │      Prompt Assembly             │
                    │   services/prompt_assembler.py   │
                    │                                  │
                    │   persona + skills + grounding   │
                    │   + Memory section:              │
                    │     "you have recall/find/note"  │
                    │     (tool list only, no data)    │
                    └──────────────┬───────────────────┘
                                   │
                                   ▼
                    ┌─────────────────────────────────┐
                    │      LLM Dispatch                │
                    │   services/llm_dispatch.py       │
                    │                                  │
                    │   chat_with_tools() loop:        │
                    │     - agent calls recall/find    │
                    │       → tool handlers query DB   │
                    │       → rendered text returned   │
                    │     - agent calls note/          │
                    │       memory_write               │
                    │       → bulletin written         │
                    │       → dream scheduled (2s)     │
                    │     - tool callback tracks       │
                    │       memory-read usage per      │
                    │       dispatch_id                │
                    └──────────────┬───────────────────┘
                                   │
                                   ▼
                    ┌─────────────────────────────────┐
                    │   Session Message Storage        │
                    │   services/session_service.py    │
                    │                                  │
                    │   add_message(role="assistant"): │
                    │     synthetic = pop_memory_used  │
                    │       (dispatch_id)              │
                    │     → synthetic=1 if recall/find │
                    │       fired this turn            │
                    └──────────────┬───────────────────┘
                                   │
                                   │  (5 min idle)
                                   ▼
                    ┌─────────────────────────────────┐
                    │  SessionIdleSummaryTask          │
                    │  heartbeat.py                    │
                    │                                  │
                    │  generate_session_bulletins():   │
                    │    - window = msgs since last    │
                    │      session_range_end           │
                    │    - prefix with N prior msgs    │
                    │      ("context only, do not      │
                    │       extract")                  │
                    │    - tag synthetic assistant     │
                    │      turns with [SYNTHETIC]      │
                    │    - write raw_transcript        │
                    │      bulletin                    │
                    └─────────────────────────────────┘
```

**Prompt assembly.** `load_workspace_prompt()` in `services/prompt_assembler.py` builds the system prompt. The memory section is intentionally minimal: it lists the tools and entity types, but does not dump entity data into the prompt. The full memory index dump was found to be ~22KB of mostly noise and is currently commented out — the agent discovers entities on demand via `recall` and `find`. The commented-out code path (`build_memory_index_text_db()`) is still available if a compact, useful index format is developed later.

**Tool registration.** `services/tool_registry.py:build_common_tools()` assembles the shared tool set for every dispatch channel. `make_memory_tools(ctx, session_key=session_key)` is called as part of the core set, alongside workspace, docs, changelog, email, session, and routine tools. The memory tools are bound to the session key so that `note` and `memory_write` derive the correct channel ID and visibility automatically. The tools returned are `recall`, `find`, `note`, `memory_write`, `memory_read`, and `memory_correct` — the agent has both the simple and explicit forms available.

**LLM dispatch and synthetic flagging.** `LLMDispatchService` in `services/llm_dispatch.py` is the single chokepoint for all LLM calls. Its tool-call callback (`_make_tool_callback`) watches for the `_MEMORY_TOOL_NAMES` set (`recall`, `find`, `memory_read`) and flips `_memory_tool_used[dispatch_id] = True` when any of them fires. After the dispatch completes, `SessionService.add_message()` calls `pop_memory_used(dispatch_id)` to determine whether the assistant message should be flagged `synthetic=1`. This flag is what the bulletin generator reads to apply the `[SYNTHETIC]` tag, which the extraction prompt then skips. The loop is closed: memory-read usage during a dispatch marks the response as synthetic, which prevents the response from being re-extracted as new ground truth in the next bulletin window.

## Key source files

| File | Purpose |
|------|---------|
| `services/memory/service.py` | `MemoryService` — bulletin CRUD, dream pipeline, search, FTS/embedding indexing, supplement, rebuild |
| `services/memory/claim_service.py` | Claim extraction from bulletins, claim CRUD, entity ID normalisation, person resolution |
| `services/memory/claim_types.py` | `CLAIM_TYPE_REGISTRY`, `ENTITY_TYPE_REGISTRY`, `render_entity()`, extraction prompt section builder |
| `services/memory/entity_templates/` | Jinja2 templates for trip, stay, connection rendering |
| `services/memory/reconciliation.py` | Tool-using reconciliation agent, `render_entity_full()`, recon tools, question persistence |
| `services/memory/bulletin_generator.py` | Legacy LLM-summary bulletin generation (used by seed) |
| `services/memory/tools.py` | `recall`, `find`, `note` implementations |
| `services/memory/embedding.py` | OpenAI embedding API + sqlite-vec similarity search |
| `services/memory/prompts.py` | Bulletin generation, claim extraction, retrieval agent prompts |
| `services/memory/channels.py` | Session key → channel ID / visibility / scope derivation |
| `services/memory/entity_resolver.py` | Entity ID normalisation, alias loading |
| `services/memory/merge.py` | Cross-entity duplicate detection and merge |
| `services/memory/cleanup.py` | Person-entity deduplication |
| `services/memory/seed.py`, `seed_email.py`, `seed_manual.py` | History replay paths for re-seeding memory |
| `services/memory_tools.py` | `make_memory_tools()` — agent-facing tool definitions |
| `services/llm_dispatch.py` | LLM call logging, memory-tool-usage tracking, `pop_memory_used()` |
| `services/session_service.py` | `add_message()` writes the `synthetic` flag |
| `services/prompt_assembler.py` | System prompt assembly, memory section injection |
| `services/tool_registry.py` | `build_common_tools()` — central tool assembly |
| `heartbeat.py` | `SessionIdleSummaryTask` — idle-session bulletin generation |
| `routers/dashboard_api.py` | All `/api/memory/...` and contact↔entity endpoints |
| `cli.py` | `bob memory ...` subcommands |
| `schemas/30*.sql` through `338_*.sql` | Migration files defining the memory tables |
