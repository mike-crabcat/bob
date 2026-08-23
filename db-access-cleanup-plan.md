# DB Access Cleanup Plan — one owner per table

Goal: every table has a small, explicit *owner set* — one writer seam plus
named read seams. Not "everything becomes a repository" (large, low-yield
rewrite) and not literally "one module" (messages already has a sanctioned
writer, session_service, distinct from its read seam, HistoryRepository).
Two sanctioned patterns plus an enforcement test that stops regression.

Reviewed 2026-08-23 by gpt-5.6-sol subagent; all 12 findings folded in.

## Current state (measured 2026-08-23, post-migration-458)

- 9 repositories exist: contacts, conversations, effects, event_log, goals,
  history (messages), participants, turns, wakeups.
- 69 files outside `repositories/` still run raw SQL.
- Worst offenders by table: memory_entities (15 files), memory_claims (13),
  contacts (13), messages (10), bindings (10), phone_calls (8),
  whatsappgroups (8), llm_call_log (7).
- Worst offenders by file: dream/store.py (49 sites — fine, it IS the owner),
  memory/service.py (40), dashboard_api/* (~90 sites across 12 files),
  heartbeat.py (17 — touches 10 different domains).

## Target architecture

**Pattern 1 — Repository** (`repositories/*.py`): for tables shared across
domains. Existing 9, plus new ones below.

**Ownership rules (per table, enforced):**
- *Writes* (INSERT/UPDATE/DELETE): strictly the owner set — usually one
  module (e.g. messages: session_service writes, HistoryRepository reads).
- *Reads*: owner set, plus repositories may JOIN other tables read-only
  (HistoryRepository already joins contacts for sender names;
  ConversationRepository.register_endpoint reads contacts — sanctioned).
- *Txn-aware APIs are mandatory*: every new repo/store method that can run
  inside `Database.transaction()` must accept a `txn` executor param
  (pattern: EventLogRepository.append, SessionService.add_message). We hit
  a real pool deadlock (add_message's bindings lookup on a second
  connection while the caller's txn held one) — audit every ported call
  site for its transaction context.

**Pattern 2 — Domain store** (single owning service module): for tables used
by one domain. The owner keeps raw SQL; everyone else calls its methods.
Already true for: dream/* (store.py), calendar, webhooks, routines, evals,
voice_sessions, skill_delegations, location_history, attention_shadow.

**Routers/heartbeat/CLI never own SQL.** They call repositories or domain
stores. Dashboard "read model" queries (joins for UI pages) move to read
methods on the owning repo/store — SQL moves, endpoints keep their shape.

## Increments (each: port + tests green + deploy, ~1 session each)

### 1. Enforcement harness first
A test (`tests/test_sql_ownership.py`) asserting a table -> owner-set map,
with an explicit allowlist of known violations that shrinks per increment.
Detector requirements (grep is too weak naive):
- Word-boundary matching (`events` must not match `turn_events`).
- Strip comments/docstrings before scanning.
- Writes checked strictly; reads against the owner set + repository layer.
- Dynamic table names (heartbeat db-growth loop, dream/store embedding
  tables) get an explicit `# sql-ownership: dynamic` annotation.
- Skip schemas/, migrations, sqlite_master/PRAGMA introspection.
There is NO push CI — this only guards deploys if it runs pre-deploy. Add
a `deploy.sh` (run suite -> commit -> push -> restart) in this increment so
the gate is structural, not habitual.

### 2a. messages bypasses
Port raw messages sites in heartbeat.py, wake_service.py, dream/store.py,
cli/replay_cmds.py, whatsapp _service.py (recovery sweep), dashboard
conversations/home/ops -> HistoryRepository read methods
(undispatched_count, per-conversation activity rollup,
restore_messages_for_turn(turn_id) — the ops.py:153 zombie-turn restore,
which legitimately joins turn_events/event_log; keep that cross-table SQL
in the repo method, named for what it does). session_service.py keeps its
writer SQL (it is the seam).

### 2b. bindings resolution moves to ConversationRepository
`bindings` belongs to ConversationRepository, not HistoryRepository —
HistoryRepository._cid() currently bypasses that ownership itself. Add a
txn-aware `resolve_cid(session_key, txn=None)` on ConversationRepository;
history/session_service/participants delegate to it (no second-connection
acquisition inside transactions). Port the inline COALESCE-bindings SQL in
heartbeat, wake_service, dream/store, replay_cmds, dashboard.

### 2c. contacts lookups
13 files do simple lookups — add `by_phone/by_email/by_jid/
display_names(ids)` to ContactRepository and port callers
(context_assembler, session_tools, group_tools, email_tools, outreach,
phone routers, heartbeat, memory/service).

Each of 2a/2b/2c is its own deploy — they cross live WhatsApp/email ingress
with different transaction contexts; do not batch them.

### 3. New repositories for hot shared tables
- `LlmCallLogRepository`: writer (llm_dispatch) + read methods for the 6
  reader files (dashboard calls/home/contacts/conversations, heartbeat,
  reflection_service).
- `PhoneCallRepository`: model *lifecycle transitions*, not generic CRUD —
  `claim_result_dispatch()` (the atomic claim-by-UPDATE at phone.py:208),
  conditional direction/status updates, voice-link mirror upsert
  (phone_calls is authoritative for Twilio calls but a projection of
  voice_sessions for voice links). Writers today: routers/phone.py,
  voice_dispatch_service, voice_session_service, subagent_service,
  heartbeat cleanup. Readers include services/phone_tools.py (don't miss
  it) and dashboard phone.py. voice_dispatch_service remains the placement
  policy owner. Twilio-callback vs media-finalization races must keep
  their current UPDATE-shape semantics.
- `GroupRepository`: whatsappgroups + whatsappgroup_members. NOTE:
  _group_events is NOT the sole writer today — memory/service.py:1135
  updates whatsappgroups.memory_entity_id. Both become repo callers; the
  repo is the only SQL owner.
- `SubagentRepository`: subagents table (5 files). Expose guarded
  lifecycle transitions (spawn/complete/fail), not arbitrary updates;
  SubagentService remains the policy owner.

### 4. Email domain store
email_threads/email_messages/email_inboxes are spread over 6 files.
Consolidate SQL into `services/email_store.py` (or fold into
email_polling_service if it's the natural owner); port email_tools,
email_delivery_service, session_agenda_service, routers/email.py,
heartbeat, dashboard conversations.py.

### 5. Memory domain consolidation
memory_entities/claims SQL is spread over 15/13 files *within* the memory
package plus 4 outsiders. Internal spread is tolerable (the package is the
owner set), so scope:
- Outsiders: heartbeat, dashboard home/memory/contacts -> route through
  memory/service.py methods.
- `services/memory_tools.py` is NOT a mere duplicate of memory/tools.py —
  it is a public tool wrapper with substantial *writes*. Its SQL must move
  behind memory/service or claim_service in this increment (it cannot
  survive allowlist removal in increment 6).
- `memory/tools.py:218-232` reads dream_* tables directly, violating
  DreamStore ownership — port to DreamStore read methods here too.

### 6. Dashboard sweep + allowlist minimization
Port remaining dashboard_api raw SQL (persona, goals+transitions, ops
turn_events, subagents, skills) to the now-existing repos/stores. Complex
multi-domain UI joins may land as read-model methods on the most-central
repo (plain dicts, small filter params). Shrink the allowlist to a short,
commented list of sanctioned cross-domain read seams — zero *write*
exceptions, but read-join seams are legitimate and stay documented in the
test itself.

## Explicitly out of scope
- Renaming cursor tables (memory_extraction_turns etc.) — cosmetic.
- schema_migrations (database.py owns it — infra).
- Rewriting domain stores as classes/repos — pattern 2 is sanctioned.

## Risks
- Read-method proliferation: dashboard pages want bespoke joins. Mitigate:
  read methods may return plain dicts and accept small filter params; do not
  force generic query builders.
- heartbeat.py touches 10 domains; port it table-by-table alongside each
  increment, not as its own big-bang.
- Increment 3's PhoneCallRepository crosses live call paths (Twilio webhooks,
  prewarm) — characterization tests for inbound/outbound call row lifecycle
  before moving SQL.
