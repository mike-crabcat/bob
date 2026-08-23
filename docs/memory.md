# Memory System

Bob's memory system is a persistent, structured knowledge base that survives across conversations. Without it, every session would start from scratch: the LLM would have no recollection of people mentioned, plans made, preferences expressed, or trips booked in earlier exchanges. The memory system closes that gap by recording facts during and after conversations, curating them into a consistent per-entity state, and surfacing them on demand through retrieval tools.

Memory lives in SQLite tables in the main database. The agent reads it through the `recall` and `find` tools (registered for every dispatch), nudges capture with `remember`, and repairs it with `memory_correct`. The heavy lifting — deciding what in a conversation is worth keeping — happens in background "silent extraction turns": small agent loops that run when a session goes idle, read the recent transcript, and write typed claims directly through a narrow tool set. A reconciliation pass then reviews entities against per-type rules and fixes inconsistencies.

Paths in this document are relative to `packages/bob-server/bob_server/`.

## Why persistent memory exists, and the tradeoffs it forces

The fundamental problem Bob solves is continuity: facts that span many sessions, channels, and weeks need to live somewhere the LLM can reach without being re-told each time. Three design decisions dominate the architecture, and each is a deliberate tradeoff.

**Claim-centric storage instead of free-text documents.** The system does not store "Mike's profile" as a paragraph of prose. It stores a set of atomic, typed claims — `food_preference: "loves Thai food"`, `spouse: person-blair-nicol`, `home_address: "42 Bondi Rd, Sydney"` — and renders them into prose on demand via per-entity-type templates. The cost is rigidity: every claim type must be declared in the registry, and the LLM has to play along. The benefit is that claims can be deduplicated, superseded, retracted, queried by type, traced to the messages that produced them, and rendered deterministically. Free-text documents give you none of that. The `claim_types.py` registry and the `entity_templates/` Jinja2 templates together define the contract.

**Extraction-on-write instead of retrieval-time reasoning.** The expensive reasoning — what facts exist in this conversation, which entity each is about, whether it conflicts with something already known — happens once, in the background, shortly after a conversation goes quiet and while the relevant transcript is small. Retrieval at runtime is then cheap: a handful of SQL queries plus one embedding lookup. The alternative (dumping raw transcripts into the prompt whenever the agent needs a fact) does not scale and hands the LLM a much larger, noisier context to reason over. The cost of extraction-on-write is occasional extraction errors and a need for downstream reconciliation; the curation pipeline is what handles both.

**Tool-loop extraction instead of transcript parsing.** Earlier versions of the system ran a two-stage pipeline: idle sessions were frozen into immutable "bulletin" documents, and a separate pass parsed each bulletin into claims. That pipeline was removed (the bulletin tables were dropped in schema `353_drop_bulletin_dream_tables.sql`). Today the extractor is itself an agent: it gets the recent message history and a tool set (`list_entities`, `get_entity`, `create_entity`, `add_claim`) and decides, with full read access to existing memory, what is genuinely new. This removes a whole storage layer and lets deduplication happen *before* a claim is written rather than after — the extractor can look before it writes. Claims carry per-message provenance (`source_messages`) instead of bulletin IDs.

## Architecture overview

Data flows from raw session messages through an extraction turn into atomic claims, which are the single source of truth. Entity rows hold identity only; everything the agent reads is a rendered view generated from claims:

```
  session_messages ─────────────────────────────────────────────────────────┐
  (raw conversation:                                                        │
   WhatsApp / email / voice,                                                │
   user + assistant turns)                                                  │
                                                                            │
     ┌──────────────────────────┐        ┌───────────────────────────┐      │
     │ idle detection           │        │ live, in-conversation     │      │
     │ SessionIdleSummaryTask   │        │ remember(hint?) tool      │      │
     │ (heartbeat.py, 60s;      │        │ /silentmem slash command  │      │
     │  idle > 5 min)           │        │ (force immediate turn)    │      │
     └────────────┬─────────────┘        └─────────────┬─────────────┘      │
                  │                                    │                    │
                  ▼                                    ▼                    │
     ┌──────────────────────────────────────────────────────────────┐      │
     │  run_silent_turn_extraction()   (services/memory/service.py) │◄─────┘
     │                                                              │
     │  • last ~30 dialogue messages, [Name] prefixes in groups     │
     │  • assistant turns that used recall/find flagged             │
     │    [SYNTHETIC]  (echo-of-memory guard)                       │
     │  • agent tool-loop on the memory model:                      │
     │      list_entities / get_entity   (look before writing)      │
     │      create_entity / add_claim    (extraction_tools.py)      │
     │  • writes a synthetic assistant message to record the turn   │
     └──────────────────────────┬───────────────────────────────────┘
                                │  claims (typed, provenance =
                                │  turn's message id)
                                ▼
     ┌──────────────────────────────────────────────────────────────┐
     │  memory_claims                    (source of truth)          │
     │  (claim_type_key, subject_id, object_id XOR value,           │
     │   status, source_messages)                                   │
     └──────┬───────────────────────────────────────────┬───────────┘
            │ identity rows for new subjects            │ active claims
            ▼                                           ▼
     ┌───────────────────────┐          ┌───────────────────────────────┐
     │  memory_entities      │          │  render_entity()              │
     │  (entity_id, type,    │────────► │  Jinja2 template or generic   │
     │   display_name,       │          │  renderer (claim_types.py)    │
     │   status,             │          └──────────────┬────────────────┘
     │   last_reconciled_at) │                         │ rendered text
     └───────────────────────┘                         ▼
                                   ┌─────────────────────────────────────┐
                                   │ memory_entities_fts (FTS5)          │
                                   │ memory_entity_embeddings (vec0)     │
                                   │ memory_aliases                      │
                                   └─────────────────────────────────────┘

     ── curation loop ─────────────────────────────────────────────────────
     MemoryReconciliationTask (heartbeat.py, hourly) + debounced triggers
     after answered questions / merges:
       reconcile_entity() — LLM with write tools checks each entity
       against per-type rules; re-renders FTS/embeddings for touched rows
```

Claims are the atoms; entities are identity plus a derived view. Nothing above the claim layer is authoritative — if the claim set is right, every rendered view, search index, and dashboard page follows from it.

## The data model

### Entities — identity rows

`memory_entities` holds only identity: `entity_id`, `entity_type`, `display_name`, `status` (`active` / `archived` / `deprecated`), `created_at`, `updated_at`, and `last_reconciled_at` (used for reconciliation backoff). There is no body column — the text the agent sees is generated on demand by `render_entity()` from the entity's active claims.

Entity IDs follow `{type}-{slug}`. The current entity types, defined in `ENTITY_TYPE_REGISTRY` in `claim_types.py`, are:

- `person`, `group`, `location`
- travel composition: `trip`, `stay` (one accommodation leg), `connection` (one transport hop), `attraction`, `dayplan` (forward-looking day itinerary), `daylog` (retrospective day record)
- `event`, `task`, `file`, `thing`, `decision`
- `self` (the singleton `self-bob`) and `relationship` (`relationship-bob-{person-slug}`, one per person Bob knows)

Person IDs use name slugs (`person-mike-cleaver`) rather than contact UUIDs, because humans are addressed by name and the slug is stable across contact-record churn. The link into the contacts table is a `contact_id` claim whose value is the contact's hex8 ID prefix. Group entities get random `group-{hex8}` IDs and are back-referenced from `whatsappgroups.memory_entity_id`. File entities are the one type with an existence rule: a file entity must carry a `file_path` claim holding a concrete workspace-relative path or URL; `deprecate_file_entities_without_path()` archives any that lose theirs.

Each registry entry carries per-type metadata in one place: a description for the extraction glossary, keywords for text detection, extraction rules injected into the extraction prompt, reconciliation rules consumed by the reconciliation agent, and flags such as `skip_expand` (don't recurse into this type during reconciliation views) and `display_name_claim` (which claim resolves a live display name).

Two special types are auto-managed. `ensure_self_entity()` (called at server startup, `main.py`) guarantees the `self-bob` row exists as the write target for Bob's model of itself. Whenever a person entity is written, `write_entity()` also creates the matching `relationship-bob-{slug}` row with a `participant` claim, so relationship observations always have a stable home.

### Claim types — the contract

`memory_claim_types` is the registry of valid keys, seeded and evolved by migrations (`317`, `321`, `334`, `342`, `361`, `362`, among others). It is mirrored in code by `CLAIM_TYPE_REGISTRY` in `claim_types.py`, which is the source of truth for behaviour: the extraction and reconciliation prompts are built from it, and the SQL table is the runtime validation gate — `write_claim()` rejects any claim whose `claim_type_key` has no row.

Each claim type declares which entity types it applies to, a description, and an example. The descriptions are load-bearing: both the extractor and the reconciler are instructed to enforce them (e.g. `preference` is for durable personal tastes only; `truth` is only for explicit user corrections of existing memory; `appearance` is durable physical description, not photo captions). Groups of claim types cover person facts (`alias`, `appearance`, `spouse`, `home_address`, `food_preference`, `assigned_identity`, …), group/event/location facts (`purpose`, `member`, `start_time`, `address`, …), trip composition (`leg`, `connection`, `attraction`, `dayplan`, `daylog`), stay and connection detail (`arrival_date`, `departure_time`, `booking_ref`, `route`, …), tasks/files/things/decisions (`owner`, `file_path`, `thing_type`, `rationale`), self-model claims (`capability`, `value`, `limit`, `self_image`, `milestone`), relationship claims (`participant`, `treatment_pattern`, `trust_signal`, `relationship_goal`, …), and cross-cutting links (`file_ref`, `truth`).

Adding a claim type means touching both registries — the Python dataclass for behaviour, a migration row for validation.

### Claims — the atomic fact

A claim is a single typed proposition about one entity, stored in `memory_claims`:

| Column | Description |
|--------|-------------|
| `id` | Generated per origin: `claim-extr-{hex8}` (extraction turns), `claim-recon-{hex8}` (reconciliation), `claim-correct-{hex8}` (agent corrections), `claim-answer-{hex8}` (answered questions), `claim-person-*` (contact linking) |
| `claim_type_key` | FK to `memory_claim_types.key`; unknown keys are rejected |
| `subject_id` | Entity the claim is about; must reference an existing entity row (orphan guard in `write_claim`) |
| `object_id` / `value` | Exactly one of: an entity reference (`spouse → person-blair-nicol`) or a scalar (`birthday = "1990-03-15"`); enforced by a table CHECK and normalised in code |
| `status` | `active`, `superseded`, `retracted`, `expired`, `disputed`, `archived`, `redundant`, `disproven`, `obsolete` — only `active` claims are read anywhere |
| `source_messages` | JSON array of `session_messages.id` values — the provenance trail pointing at the extraction turn(s) that recorded the claim |
| `visibility` / `scope` | Access-control tags (see *Visibility and scope*) |
| `superseded_by` | JSON array of claim IDs or the label `"reconciliation"` |

`write_claim()` in `claim_service.py` deduplicates on write: if an active claim with the same `(claim_type_key, subject_id, object_id|value)` already exists, the new provenance is merged into the existing row instead of inserting a duplicate. This is what lets the same fact be re-observed across overlapping windows and collapse into one claim with multiple source messages. A separate `validate_claim_for_write()` rejects structurally wrong claims before they land (currently: `file_ref` claims whose `object_id` is not a `file-*` entity).

Claims are never hard-deleted. Correction means superseding: the old row flips to `superseded` and, where a replacement exists, a new active claim is written — preserving the full history of what was believed and when.

### Provenance — extraction turns and source messages

Every extraction run is recorded in `memory_extraction_turns` (`id`, `session_key`, `message_id` of the synthetic assistant message that represents the turn, `ran_at`, `claims_created`). Claims written during a turn carry that message id in `source_messages`, so any claim traces back to the exact turn — and transcript — that produced it. This is the successor of the old bulletin provenance (`source_bulletins`, dropped in schema `353`).

Claims written by reconciliation, agent corrections, and answered questions have empty `source_messages`; renderers label these `[source: none — inferred]`, which reconciliation treats as weaker than transcript-grounded claims when resolving conflicts.

### Rendered views — from claims to text

`render_entity()` in `claim_types.py` turns a claim set into the prose the agent and dashboard see; there is no LLM in the rendering path. Entity types with a Jinja2 template in `services/memory/entity_templates/` (`trip.md`, `stay.md`, `connection.md`, `daylog.md`) get rich rendering: claims grouped by type, referenced entities recursively resolved from the database (with a cycle guard), legs/connections sorted by date. All other types fall back to the generic renderer, which lays claims out as labelled lines and bullet lists according to the in-code `_ENTITY_TEMPLATES` ordering, appending any *orphan claims* (types not in the template) under their own heading. The output is deterministic for a given claim set, and the same text is used for FTS indexing, embedding generation, dashboard display, and tool responses — an entity looks identical everywhere.

### Derived indexes

Three lookup structures are derived from entity state:

- `memory_aliases` — display name (and lowercase variant) → entity ID, refreshed on `write_entity()`.
- `memory_entities_fts` — FTS5 over `(entity_id, display_name, rendered_body)`; a standalone (non-triggered) table, so the application must refresh rows explicitly via `update_entity_fts()`.
- `memory_entity_embeddings` — sqlite-vec `vec0` table mapping entity ID to a 1536-dimension `text-embedding-3-small` vector of the rendered body (`embedding.py`).

`update_entity_fts()` re-renders the entity, replaces the FTS row, and upserts the embedding. It is called by `memory_correct` actions, entity writes, merges, and — importantly — by the reconciliation pass for every entity it touches. Claims written by a silent extraction turn are therefore searchable by exact ID immediately, but their FTS/embedding rows catch up when the hourly reconciliation task refreshes the entity (normally within about an hour).

## Authoring paths — how facts enter memory

There are several ways a fact becomes a claim. All of them write through the same `write_claim()` gate, so validation and deduplication apply uniformly.

### Idle silent-turn extraction

The main path requires no agent action at all. `SessionIdleSummaryTask` in `heartbeat.py` runs every heartbeat cycle (60 s by default) and looks for sessions that (a) have dialogue messages newer than the session's last extraction turn (`MAX(memory_extraction_turns.ran_at)`), and (b) have been idle longer than `BOB_SESSION_SUMMARY_IDLE_MINUTES` (default 5). `subagent:%` sessions are excluded. For each, it calls `MemoryService.run_silent_turn_extraction()`:

1. **Guard.** Skips if there are no undigested messages (checked again under the session lock to avoid races with a concurrent turn).
2. **Prompt assembly.** `build_silent_turn_prompt()` (`prompts.py`) plus the entity-type/claim-type glossary from `build_extraction_prompt_section()`, plus a channel-context block — for group chats, a participant roster; for DMs, the other participant's name — with explicit instruction to look up existing `person-*` / `group-*` entities before recording anything.
3. **History rendering.** The last ~30 dialogue messages (`HistoryRepository.recent_dialogue`) as native role-structured messages: user turns prefixed `[Name]` in groups for attribution, assistant turns that were generated with memory recall prefixed `[SYNTHETIC]`, `NO_REPLY` placeholders dropped.
4. **Tool loop.** `chat_with_tools()` on the memory model (`BOB_OPENAI_MEMORY_MODEL`, falling back to the default model), category `memory_silent_turn`, up to 25 iterations, with the extraction tool subset from `extraction_tools.py`.
5. **Recording.** Claims are written during the loop; every claim gets the turn's synthetic message id in `source_messages`. The turn itself is stored as a synthetic assistant message (`msg-extr-*`, metadata `{"memory_extraction_turn": true, "trigger": ...}`) so it appears in the transcript and is itself skippable by future extraction, and a row lands in `memory_extraction_turns`.

The turn is serialized against live replies via `SessionDispatchGate` — the same per-session lock the dispatch path uses — so extraction never interleaves with an in-flight reply.

The extraction prompt encodes the quality bar: only form memories from *other people's* messages, never Bob's own; weight replies to `[SYNTHETIC]` lines as corroboration rather than fresh assertion; do not record jokes, hypotheticals, scheduling chatter, or facts attributed to the wrong person; consolidate instead of accumulating near-duplicates; "when in doubt, omit". Several long sections enumerate known miscategorization traps (`preference` as a junk drawer, `truth` as a fact bucket, photo captions as `appearance`, changelog entries as `milestone`).

### The `remember` tool and `/silentmem`

Mid-conversation, the agent can call `remember(hint?)`. This does not write anything itself — it queues a deferred extraction turn (`queue_remember_extraction`) that blocks on the session lock until the current reply finishes and is stored, then runs the same silent-turn flow with `force=True` and an optional topic hint. The WhatsApp bridge exposes the same thing to operators as `/silentmem`, which runs immediately and reports back what was recorded.

`/verbose on|off|status` toggles per-session verbose notices (`conversations.policy_json.memory_verbose`): when on, any extraction turn that created entities or claims posts a `[memory]` system notice into the chat and publishes a `memory.verbose_notice` event on the event bus for active transports to deliver.

### Live corrections — `memory_correct`

When the agent learns memory is wrong ("actually it was 2 stops in Paris, not 1"), the `memory_correct` tool (in `services/memory_tools.py`) applies structured fixes: `remove_entity`, `remove_claim`, `add_claim`, `replace_claim`, `set_truth`, `rename_entity`, `create_entity`. Every action requires a `reason`. Removals supersede claims and write a `truth` claim recording why, which prevents extraction from simply re-creating the bad data; `rename_entity` rewrites all claim references and refreshes FTS/embeddings. This is the agent-facing equivalent of the reconciliation tool set, scoped to corrections the user just stated.

### Person entities from the contacts directory

Separately from conversation extraction, person entities are seeded structurally: `ensure_person_entry()` (called from `channel_policies.py` on session setup and from `contact_tools.py` when contacts are created) creates a `person-{slug}` row plus a `contact_id` claim if none exists, looking up by `contact_id` claim first (survives renames) and slug second. Contact renames propagate to the linked entity's `display_name` (`sync_person_display_name_for_contact`), and soft-deleting a contact retires its `contact_id` claims (`retire_contact_id_claim`).

### Preventing re-ingestion and feedback loops

Extraction-on-write has two classic failure modes: extracting the same fact twice from overlapping windows, and treating the agent's own recollections as new ground truth. The current design addresses both structurally.

**Turn watermark + look-before-write.** Each extraction turn records its `ran_at`; idle detection only fires for sessions with messages newer than that watermark, so a quiet conversation is not re-mined. When a turn *does* run, it renders the last ~30 messages regardless of the watermark — the window deliberately overlaps older, already-extracted turns so the extractor has context. Re-ingestion is prevented by the tool-loop shape itself: the prompt's first rule is "before writing, look" (`list_entities` / `get_entity` to see what is already known), and `write_claim()` content-dedup collapses any re-observed fact into the existing row, adding provenance rather than a duplicate.

**Synthetic-echo flagging.** When the agent calls `recall` or `find` during a dispatch, the resulting assistant message is an echo of existing memory — not ground truth. `LLMDispatchService` tracks this: a tool-call callback flips `_memory_tool_used[dispatch_id] = True` whenever a memory-read tool fires, and `SessionService.add_message()` pops that flag when storing the assistant response, writing `synthetic=1` on the message. The extraction history renderer prefixes those messages with `[SYNTHETIC]`, and the extraction prompt's corroboration rule says a person *replying* to a `[SYNTHETIC]` message confirms existing memory at most once, at lower confidence — never a brand-new entity. The loop is closed: memory-read usage during a dispatch marks the reply synthetic, which stops the reply from being re-extracted as new truth in the next extraction turn.

## The curation pipeline

Raw extraction turns produce claim state; two further stages turn that into clean, consistent entity state. Each stage has a distinct responsibility and a distinct relationship to the LLM.

### Stage 1 — extraction (additive only)

The silent extraction turn *is* stage 1. Its tool set (`extraction_tools.py`) is deliberately narrow: `list_entities` and `get_entity` (shared read-only tools from `entity_tools.py`) plus `create_entity` and `add_claim`. There is no retract, supersede, delete, or merge — extraction only records what was said; it never repairs existing state. Add-on validation runs before each write (`validate_claim_for_write`), and `add_claim` returns a recoverable error if the subject entity does not exist yet, prompting the model to `create_entity` within the same turn instead of dropping an orphan claim.

### Stage 2 — reconciliation (consistency and repair)

`reconcile_entity()` (`reconciliation.py`) is the repair stage and the only automated writer with destructive tools. It is a tool-using agent loop, given:

- The entity rendered via `render_entity_full()` — a recursive view (depth 2) that expands entity-ref claims into child entities, skipping `skip_expand` types, with a provenance tag on every claim (`[source: N messages]` or `[source: none — inferred]`).
- The per-type `reconciliation_rules` from the entity type registry — concrete, checkable rules like "Stay date ranges must not overlap", "Each distinct accommodation MUST be its own stay entity", "A connection SHOULD be referenced by a trip's connection claim".
- The claim-type glossary for the entity type, with instructions to retract claims whose values violate their type's definition even when superficially well-formed.
- Previously answered questions for the entity, treated as ground truth.

And the reconciliation tool set (`make_reconciliation_tools`): `list_entities`, `get_entity`, `add_claim`, `retract_claim`, `supersede_claim_tool`, `create_entity`, `delete_entity`, `merge_entities`. The prompt's stance is "prefer acting over asking" — the agent applies fixes directly via tools and returns a JSON summary of `issues` and `questions`. Only genuinely ambiguous cases (e.g. two overlapping stays that might be intentional) become questions, persisted to `memory_questions` for a human. Answering a question (dashboard or API) writes the answer as a `truth` claim and queues the entity for re-reconciliation via a 2-second debounced trigger, so the agent acts on it rather than re-asking.

Before each reconciliation run, `deprecate_file_entities_without_path()` sweeps file entities that lost their valid `file_path`. After the run, FTS and embeddings are refreshed for every entity the tool calls touched — this is also how freshly extracted claims become full-text/embedding searchable.

Earlier versions had a third stage (supplement: LLM gap-filling of inferable claims such as a stay's arrival date implied by a flight). It has been removed; reconciliation's rules can still add such claims, but there is no dedicated inference pass.

### When reconciliation runs

Two triggers:

- **Debounced re-reconciliation** (`MemoryService._schedule_reconciliation`, 2 s) after a question is answered or entities are merged.
- **The hourly heartbeat task** (`MemoryReconciliationTask` in `heartbeat.py`, throttled to once per hour, gated by `BOB_RECON_DAILY_BATCH_ENABLED`): it selects up to `BOB_RECON_DAILY_BATCH_MAX_ENTITIES` (default 50) entities with claims or rows created in the last 24 hours, filters out any reconciled within `BOB_RECON_MIN_INTERVAL_HOURS` (default 6, via `memory_entities.last_reconciled_at`), and reconciles the rest. The min-interval backoff prevents the same busy entity from burning model calls every hour.

## Retrieval — how the agent accesses memory

The agent sees memory through two tools registered by `make_memory_tools()` for every dispatch. The same primitives back the dashboard search and the CLI query command.

### `recall(query)`

The primary retrieval tool. `recall()` in `services/memory/tools.py` resolves the query through a four-step cascade:

1. **Exact entity ID** — direct lookup in `memory_entities`. `recall("person-mike-cleaver")` lands here.
2. **Alias lookup** — case-insensitive match in `memory_aliases`. `recall("Mike")` lands here when "Mike" is an alias.
3. **Embedding search** — the query is embedded and matched against `memory_entity_embeddings` by cosine distance (threshold 1.2, top 5). The closest match becomes the primary result; the remaining matches are rendered in full and appended below a `---` separator. This is what makes natural-language queries like "what kind of car does david have" find `thing-ebike` even when neither word appears verbatim in the rendered body.
4. **FTS5 fallback** — query tokens quoted and AND-joined against `memory_entities_fts`.

The resolved entity's active claims are rendered via `render_entity()`, then two augmentation blocks are appended: a "Referenced by:" section listing reverse references (claims where this entity is the `object_id` — the graph-traversal affordance, since `recall` on any referenced ID expands the neighbourhood), and an "Open dream items:" block when the entity is linked to active dream plans or resolutions (see *Integration points*). ID lookup is the second graph mechanism: entity-ref claims make the claim graph navigable one `recall` at a time, and `render_entity_full()` walks the same edges for reconciliation.

### `find(entity_type, claim_type_key?, value?)`

Structured search: all active entities of a type, optionally filtered by claim type and value substring. Used for queries like "list all trips" or `find("dayplan", "date", "2026-06-30")`.

### How retrieval results reach the conversation

Retrieval results come back as plain text in the tool response; the agent folds them into its reply. Nothing is pre-injected: the system prompt contains only the tool list and guidance (a full memory index dump used to be appended and was disabled — the agent discovers entities on demand). The one proactive injection is channel context: `ContextAssembler.person_profile()` adds a person-profile pointer for DM sessions, and `group_memory_hint()` tells group sessions which `group-{hex8}` entity holds the group's accumulated knowledge and to `recall` it. Because using `recall`/`find` flags the dispatch's reply synthetic, retrieval itself participates in the anti-echo loop described above.

## Visibility and scope

Every claim carries a `visibility` field (`private`, `contact`, `group`, `channel`, `public`; default `channel`) and a `scope` JSON array for finer-grained tags. `channels.py` provides the derivation helpers (`derive_visibility`, `derive_scope`) that map a session key — e.g. `agent:main:whatsapp:group:12036342829458` — to a channel ID, a default visibility, and scope tags such as `["public", "group-12036342829458"]`, and a `QueryContext` model (`actor`, `channel_id`, `allowed_scopes`) exists for callers that want to enforce filtering.

The honest current state: Bob is a single-operator assistant, so retrieval does **not** yet filter by caller context — `recall` and `find` return active claims regardless of visibility, and the silent-turn extractor writes claims with the default visibility rather than deriving per-session scope. The visibility/scope columns are the data contract for future enforcement (e.g. keeping a fact learned in one group private to that group): any filtering layer would be added at the retrieval boundary, keyed on `QueryContext`, without schema changes.

## Operability

The system is designed to be inspectable and repairable without a full rebuild. Tooling lives in the CLI (`cli/memory_cmds.py`, exposed as `bob memory ...`), the dashboard API (`routers/dashboard_api/memory.py`), and the dashboard UI (`ui_app/src/routes/memory/`).

### CLI

- `bob memory reconcile [IDs...] [--all] [--render]` — run reconciliation on specific entities or all active ones; `--render` dumps the recursive entity view (exactly what the reconciliation agent sees) without any LLM call — the first stop when debugging what the agent was given.
- `bob memory merge [--dry-run]` — detect duplicate entities via embedding similarity plus an LLM yes/no confirmation, and merge them.
- `bob memory reindex` — rebuild the FTS index from current claim state (no LLM calls).
- `bob memory validate` — structural checks (missing display names / entity types).
- `bob memory cleanup-contacts [--dry-run]` — fold duplicate person entities onto canonical contact IDs and rewire references.
- `bob memory query QUESTION [--type T]` — natural-language search through the same FTS/embedding path as the dashboard.
- `bob memory model-override-set ENTITY_ID MODEL [--reason ...]` / `model-override-remove ENTITY_ID` / `model-override-list` — manage per-entity reconciliation model overrides in `recon_model_overrides`.

There is no whole-store rebuild command anymore — repair is incremental: reindex for indexes, merge/cleanup-contacts for duplicates, reconcile for entity state, `memory_correct` (agent-side) or direct SQL for individual bad rows. Claim history is never destroyed, so a wrong "fix" can itself be superseded.

### Dashboard API

Under `/api/memory/...`, authenticated by the dashboard secret:

- `GET /api/memory/stats` — entity counts by type plus recent entities.
- `GET /api/memory/search?q=...` — hybrid FTS-then-embedding search; logged to `memory_search_log`. `GET /api/memory/searches` returns recent history.
- `GET /api/memory/entities[?type=...]` — entity list with claim counts and per-type summary fields.
- `GET /api/memory/entities/{id}` — rendered body plus all active claims.
- `GET /api/memory/claims?type=&subject_id=&status=` — claim browser.
- `GET /api/memory/questions?status=open`, `POST .../questions/{id}/answer`, `POST .../questions/{id}/dismiss` — the reconciliation question queue; answering writes a `truth` claim and queues re-reconciliation.
- `POST /api/memory/entities/merge` — merge two entities from the UI.
- `GET /api/contacts/{id}/entity` — the person entity for a contact, located by `contact_id` claim or name-slug fallback.

### Dashboard UI

The `/memory` page has five tabs: **entities** (list + detail with rendered body, claims, and merge controls), **pipeline** (a placeholder noting extraction is per-turn silent — there is no queue or dream log for memory), **search** (live hybrid search with history), **stats** (entity counts by type), and **qa** (open and answered reconciliation questions). Contact detail pages embed the linked person entity.

### Per-entity model overrides

Some entities are harder to reconcile than others — a multi-leg trip with merged stays needs more model than a person with three claims. `resolve_reconciliation_model()` picks the model in order: a per-entity row in `recon_model_overrides`; else the entity type listed in `BOB_RECON_LARGE_MODEL_TYPES` (which routes to `openai.default_model`); else the small memory model (`BOB_OPENAI_MEMORY_MODEL`, falling back to the default). The CLI `model-override-*` commands manage the table directly.

## Integration points

Memory plugs into the rest of Bob at four seams: tool registration, prompt assembly, LLM dispatch (including the synthetic-flag loop), and the heartbeat scheduler. The `self`/`relationship`/`assigned_identity` claim types additionally give other subsystems (persona, dream) stable anchors inside memory.

```
  ┌────────────────────────────────────────────────────────────────────┐
  │                        PROMPT ASSEMBLY                             │
  │  prompt_assembler.load_workspace_prompt()                          │
  │    └─ "## Memory" section: recall / find / remember /              │
  │       memory_correct descriptions + "when to consult memory"       │
  │       guidance (tool list only — no entity data dumped)            │
  │  context_assembler.ContextAssembler                               │
  │    ├─ person_profile(contact_id)      (DM sessions)                │
  │    └─ group_memory_hint(session_key)  (group sessions: recall the  │
  │                                        group-{hex8} entity)        │
  └──────────────────────────────┬─────────────────────────────────────┘
                                 │ system prompt
                                 ▼
  ┌────────────────────────────────────────────────────────────────────┐
  │                     LLM DISPATCH (llm_dispatch.py)                 │
  │  tool_registry.build_common_tools()                                │
  │    └─ memory_tools.make_memory_tools(session_key)                  │
  │         → recall, find, remember, memory_correct                   │
  │                                                                    │
  │  chat_with_tools() loop:                                           │
  │    agent calls recall/find ──► SQL / FTS / embedding ──► rendered  │
  │                                text returned as tool output        │
  │    agent calls remember ─────► deferred extraction task queued     │
  │    agent calls memory_correct ─► claims superseded / truth written │
  │                                                                    │
  │  tool callback watches _MEMORY_TOOL_NAMES = {recall, find}         │
  │    → _memory_tool_used[dispatch_id] = True                         │
  └──────────────┬─────────────────────────────────────────────────────┘
                 │ after dispatch
                 ▼
  ┌────────────────────────────────────────────────────────────────────┐
  │  SessionService.add_message(role="assistant")                      │
  │    synthetic = pop_memory_used(dispatch_id) → session_messages.    │
  │    synthetic=1 when the reply echoed memory                        │
  └──────────────┬─────────────────────────────────────────────────────┘
                 │ session idle > 5 min, new msgs since last turn
                 ▼
  ┌────────────────────────────────────────────────────────────────────┐
  │  heartbeat.py tasks                                                │
  │    SessionIdleSummaryTask (60s)  → run_silent_turn_extraction()    │
  │        [SYNTHETIC] prefixes on synthetic turns; look-before-write; │
  │        claims written with per-turn provenance                     │
  │    MemoryReconciliationTask (hourly) → reconcile_entity() on       │
  │        entities touched in last 24h → FTS/embedding refresh        │
  └────────────────────────────────────────────────────────────────────┘
```

**Tool registration.** `services/tool_registry.py:build_common_tools()` assembles the shared tool set for every dispatch channel and includes `make_memory_tools(ctx, session_key=session_key)` alongside workspace, docs, and email tools. Binding to the session key lets `remember` queue the deferred turn against the right session.

**Prompt assembly.** `load_workspace_prompt()` injects a compact Memory section describing the four tools and when to consult them. Channel-level context blocks come from `ContextAssembler` (`context_assembler.py`): the person-profile pointer for DMs and the group-memory hint for groups.

**LLM dispatch and the synthetic flag.** `LLMDispatchService` is the single chokepoint for LLM calls. Its tool-call callback watches for memory-read tools and flags the dispatch; `SessionService.add_message()` consumes the flag when persisting the reply, and the extraction history renderer turns it into the `[SYNTHETIC]` prefix the extractor is told to treat as corroboration-only. All memory LLM passes run on the memory model by default and are tagged with distinct `call_category` values (`memory_silent_turn`, `memory_reconciliation`) for cost and log filtering.

**Heartbeat scheduler.** `main.py` registers `SessionIdleSummaryTask` and `MemoryReconciliationTask` alongside the other background tasks, and calls `ensure_self_entity()` at startup.

**Dream cross-links.** The dream system (a separate reflective-planning subsystem, `services/dream/`) links its plans and resolutions to memory entities via `dream_item_links`; `recall` surfaces open items for the recalled entity, so the agent sees pending intentions alongside stored facts. Dream is a consumer of memory, not part of the memory pipeline.

## Key source files

| File | Purpose |
|------|---------|
| `services/memory/service.py` | `MemoryService` — silent-turn extraction, entity CRUD, reconciliation scheduling, person/contact helpers, search, index rebuilds |
| `services/memory/claim_service.py` | `write_claim()` (validation, orphan guard, dedup), claim queries, entity-ID normalisation, `update_entity_fts()` |
| `services/memory/claim_types.py` | `CLAIM_TYPE_REGISTRY`, `ENTITY_TYPE_REGISTRY`, `render_entity()`, extraction prompt glossary builder |
| `services/memory/entity_templates/` | Jinja2 templates for trip, stay, connection, daylog rendering |
| `services/memory/extraction_tools.py` | Additive tool subset for silent extraction turns (provenance-threaded) |
| `services/memory/reconciliation.py` | Reconciliation agent: prompt, tools, `render_entity_full()`, questions, model resolution, backoff filter |
| `services/memory/entity_tools.py` | Shared read-only `list_entities` / `get_entity` tools |
| `services/memory/tools.py` | `recall()` resolution cascade and `find()` |
| `services/memory/embedding.py` | OpenAI embeddings + sqlite-vec similarity search |
| `services/memory/prompts.py` | Silent-turn system prompt (quality rules, miscategorisation traps) |
| `services/memory/models.py` | `Claim`, `EntityDocument`, `QueryContext` dataclasses; status/visibility enums |
| `services/memory/channels.py` | Session-key → channel ID / visibility / scope derivation helpers |
| `services/memory/merge.py`, `cleanup.py` | Duplicate detection/merge; contact-entity cleanup |
| `services/memory_tools.py` | `make_memory_tools()` — agent-facing `recall`, `find`, `remember`, `memory_correct` |
| `services/llm_dispatch.py` | Dispatch chokepoint; memory-tool-usage tracking (`pop_memory_used`), `memory_model` |
| `services/session_service.py` | `add_message()` writes the `synthetic` flag |
| `services/prompt_assembler.py`, `services/context_assembler.py` | Memory prompt section; person/group memory hints |
| `services/tool_registry.py` | `build_common_tools()` — central tool assembly |
| `heartbeat.py` | `SessionIdleSummaryTask` (idle extraction), `MemoryReconciliationTask` (hourly curation) |
| `routers/dashboard_api/memory.py` | `/api/memory/*` endpoints |
| `cli/memory_cmds.py` | `bob memory ...` subcommands |
| `ui_app/src/routes/memory/index.tsx` | Dashboard memory page |
| `schemas/30*.sql` … `36*.sql` | Migrations defining the memory tables (`307` origin; `318` claims v2; `320/322` indexes; `342` self/relationship; `346` silent-turn provenance; `353` bulletin drop; `361/362` recent claim types) |
