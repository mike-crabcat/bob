# Dream v2 — reflective self-improvement & proactive plans

Status: **implemented** (phases 1–4, 2026-08-16) · Branch: `dream-v2` · Written 2026-08-16
Supersedes the removed dream pipeline (bulletin→claim batch dream, dropped in schema 353).

## Goal

When Bob has been idle, a dream run reviews the sessions that were active since the last
dream and produces two kinds of durable artifacts:

- **Resolutions** — "I did this badly and should improve": an evidence-cited record of a
  shortcoming observed in Bob's own behaviour. Collected over time; later derivation into
  self-improvement programs (out of scope here, schema reserves the link).
- **Plans** — "this was left hanging": an incomplete task or implicit commitment detected in
  conversation (e.g. people discussed catching up but nothing was arranged), with a proposed
  way Bob can assist, to drive proactive action.

Every run also writes a **dream journal** — what it observed, what it created/merged/expired,
and why — for the operator to audit in the dashboard.

## Non-goals

- **Program derivation** (clustering resolutions → improvement programs). Collected resolutions
  are the prerequisite; `program_id` is reserved on `dream_resolutions` but no `dream_programs`
  table yet.
- **Task execution engine.** The legacy tasks/projects engine was removed (commit `8c3d016`);
  dream plans carry their own lightweight lifecycle. If an execution engine is ever rebuilt,
  it will be informed by what dream plans turn out to need. `dream_plans.task_id` is reserved
  but nothing writes it.
- **Autonomous outbound action.** Tier 2 actions (calls, messages, emails to real people)
  require explicit operator enablement; the default build only *surfaces* plans.
- **Legacy table cleanup.** Tracked separately in `TODO.md`; do not couple.

## Settled design decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | Resolutions/plans live in **first-class tables**, not as memory entity types | Memory's invariant is derived-from-bulletins/rebuildable; resolutions/plans have mutable workflow state a `memory rebuild` would wipe or duplicate. Linking to memory entities gives association without inheritance. |
| D2 | Association via a **link table** (`dream_item_links`) referencing `session_key` and `entity_id` (`person-*`, `group-*`) | Many-to-many: one plan can involve a group plus specific individuals. Reverse lookup ("open plans for this person") is one indexed query. |
| D3 | Plans are **proposals with their own lifecycle**, not tasks | No engine exists to graduate into (see non-goals). Status flow: `draft → proposed → approved → actioned → completed / expired / dismissed`. |
| D4 | Every dream has **two halves**: retrospective (new sessions) + prospective (review prior items) | Without the prospective half the system is write-only; plans accumulate forever and resolutions never resolve. |
| D5 | **Dedup/merge via embeddings**, not fresh creation | An LLM reflecting every idle interval regenerates near-identical items each cycle. Repeated independent observations should *strengthen* an item (`observation_count`), not duplicate it. |
| D6 | Resolutions must be **evidence-grounded and falsifiable** | Ungrounded self-criticism produces hallucinated, unfalsifiable resolutions. Each resolution carries behaviour + trigger condition + success signal, all validated against cited messages. |
| D7 | **Draft mode first** — items written but nothing surfaced to the agent, no actions | Matches careful-rollout practice: verify quality on real samples in the dashboard before the agent or anyone else acts on dream output. |
| D8 | Dream journals are **not bulletins** | A journal talking about people would re-enter claim extraction and risk feedback loops. Revisit deliberately later if a "second-chance extraction net" is wanted. |
| D9 | **All dream LLM calls run on the memory model** (`openai.get_memory_model()`) | Dreaming is background housekeeping, never user-facing — it inherits the low-cost slot for every pass (review, prospective, synthesis) with no per-pass escalation or overrides. |
| D10 | **Approved plans are announced** in their linked session — one natural message offering help, batched per session | Approval that nobody hears about is inert; the announcement is the proactive payoff of a plan. Batching + `announced_at` guard prevent spam. |
| D11 | **`/autoplan on` enables auto-approval from day one**, via the trusted-contact slash-command path, backed by a runtime DB toggle | Operator's explicit choice to skip manual plan approval early. Guardrails: auto-approval still never enables Tier 2 outreach, and per-run/per-session caps still apply. Slash command needs a restart-free store, hence DB-backed config rather than env. |
| D12 | **Participants change or cancel plans conversationally** via session-bound agent tools; session binding *is* the permission model | The most common response to an announcement is conversational ("we already sorted it", "make it Sunday"). Tools bound to the session key can only touch plans linked to that session — participants were party to the conversation that created the plan, so they may close or amend it; nobody else can. No slash syntax for regular people. |

## Data model

Migrations: `357_dream_tables.sql`, `358_dream_deferred_candidates.sql` (cap rollover),
`359_dream_embeddings_cosine.sql` (fix: vec0 tables default to L2 distance — the table is
recreated with `distance_metric=cosine` to match the threshold semantics; `bob dream reindex`
re-embeds after metric changes). The legacy-table drop moved to 360 — see `TODO.md`.

```sql
CREATE TABLE dream_runs (
    id TEXT PRIMARY KEY,                    -- dream-YYYY-MM-DD-hex8
    started_at TEXT NOT NULL,
    finished_at TEXT,
    window_start TEXT NOT NULL,             -- inclusive
    window_end TEXT NOT NULL,               -- exclusive; cursor for next run
    status TEXT NOT NULL CHECK (status IN ('running','complete','failed')),
    trigger TEXT NOT NULL CHECK (trigger IN ('heartbeat','manual','cli')),
    model TEXT NOT NULL,
    sessions_reviewed_json TEXT NOT NULL DEFAULT '[]',  -- [{session_key, new_messages}]
    stats_json TEXT NOT NULL DEFAULT '{}',  -- candidates/rejected/deduped/expired/token spend/llm call ids
    journal_text TEXT NOT NULL DEFAULT '',  -- narrative: observed / created / merged / expired, with reasons
    error TEXT
);

CREATE TABLE dream_resolutions (
    id TEXT PRIMARY KEY,                    -- resolution-hex8
    title TEXT NOT NULL,
    behaviour TEXT NOT NULL,                -- the observable behaviour, good or bad
    trigger_condition TEXT NOT NULL,        -- when/where it applies
    success_signal TEXT NOT NULL,           -- what a future dream checks to mark kept
    status TEXT NOT NULL CHECK (status IN
        ('draft','open','in_program','kept','dropped','stale')),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    observation_count INTEGER NOT NULL DEFAULT 1,
    evidence_json TEXT NOT NULL DEFAULT '[]', -- [{run_id, session_key, message_at, excerpt, kind}]
    source_run_id TEXT NOT NULL REFERENCES dream_runs(id),
    program_id TEXT                         -- reserved, NULL for now
);

CREATE TABLE dream_plans (
    id TEXT PRIMARY KEY,                    -- plan-hex8
    title TEXT NOT NULL,
    what_was_discussed TEXT NOT NULL,
    proposed_action TEXT NOT NULL,          -- the concrete next step for the human(s)
    assistance_method TEXT NOT NULL,        -- how Bob can assist (draft msg, find a date, place a call…)
    autonomy_tier INTEGER NOT NULL DEFAULT 1 CHECK (autonomy_tier IN (1,2)),
    status TEXT NOT NULL CHECK (status IN
        ('draft','proposed','approved','actioned','completed','expired','dismissed')),
    approved_by TEXT,                       -- 'operator' | 'auto' | NULL
    approved_at TEXT,
    announced_at TEXT,                      -- NULL until announced in its session
    reannounced_at TEXT,                    -- set when the single follow-up is spent
    evidence_json TEXT NOT NULL DEFAULT '[]',
    source_run_id TEXT NOT NULL REFERENCES dream_runs(id),
    due_hint TEXT,                          -- free-text/date from conversation, if any
    task_id TEXT,                           -- reserved, NULL for now
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE dream_item_links (
    item_type TEXT NOT NULL CHECK (item_type IN ('resolution','plan')),
    item_id TEXT NOT NULL,
    session_key TEXT,
    entity_id TEXT                          -- person-*/group-* memory entities
);
CREATE INDEX idx_dream_links_entity ON dream_item_links(entity_id, item_type);
CREATE INDEX idx_dream_links_session ON dream_item_links(session_key, item_type);
CREATE INDEX idx_dream_resolutions_status ON dream_resolutions(status);
CREATE INDEX idx_dream_plans_status ON dream_plans(status);
CREATE INDEX idx_dream_runs_window ON dream_runs(window_end);
CREATE INDEX idx_dream_plans_pending_announce ON dream_plans(status, announced_at);

-- Per-session review cursors (pattern of memory_extraction_turns): how far each
-- session has been reviewed. Sessions qualify for a dream when they have messages
-- newer than their cursor.
CREATE TABLE dream_session_review (
    session_key TEXT PRIMARY KEY,
    last_reviewed_message_at TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES dream_runs(id),
    updated_at TEXT NOT NULL
);

-- Runtime toggles settable without restart (slash command / dashboard).
-- Env settings are boot defaults; values here override.
CREATE TABLE dream_config (
    key TEXT PRIMARY KEY,                   -- e.g. 'auto_approve_plans'
    value TEXT NOT NULL,                    -- JSON-encoded
    updated_at TEXT NOT NULL
);
```

Embeddings for dedup reuse the embed call in `services/memory/embedding.py`
(`text-embedding-3-small`, 1536-dim) but need a **dedicated `vec0` table**
`dream_item_embeddings(item_id, embedding)` plus a new similarity query — the existing
`search_similar()` is hardwired to `memory_entity_embeddings`.

## Dream run lifecycle

Registered as a heartbeat task (`DreamTask` in `heartbeat.py`), single-flight via an asyncio
lock. A run:

1. **Claim window.** Insert `dream_runs` row `status='running'` with descriptive
   `window_start`/`window_end`. **Review coverage is tracked per session**, not by the
   global window: `dream_session_review(session_key, last_reviewed_message_at, run_id)`
   records how far each session has been reviewed (same pattern as
   `memory_extraction_turns`). A session qualifies when it has messages newer than its
   cursor; first sight of a session is bounded by `first_run_lookback_days` (default 14)
   so run #1 doesn't queue months of history. Partial work in failed runs is naturally
   re-reviewed next run; dedup absorbs it. Overflow sessions beyond `max_sessions_per_run`
   simply keep their old cursor — nothing is silently dropped.
2. **Collect sessions.** Sessions (excluding `subagent:%`) with messages newer than their
   cursor, **newest activity first** (fresh sessions are the valuable ones; the stale
   first-run backlog mops up over later runs — dedup is order-symmetric, so nothing
   breaks), capped at `max_sessions_per_run`. Skip sessions below
   a minimum new-message count.
3. **Retrospective pass** (per session, one LLM call, `call_category='dream_review'`):
   - Input: transcript window (**numbered lines**, message list capped in length; longer
     sessions get per-window pre-summaries) + participant roster (contact→person mapping,
     as in claim extraction) + group entity where applicable.
   - Output: JSON candidates — resolutions `{title, behaviour, trigger_condition,
     success_signal, evidence[]}` and plans `{title, what_was_discussed, proposed_action,
     assistance_method, autonomy_tier, due_hint?, evidence[], related_entities[]}`.
     Evidence cites **line indices**, not timestamps — LLMs cannot reliably echo ISO
     timestamps, indices validate exactly.
   - **Evidence validation in code:** every cited index must exist in the transcript
     excerpt shown to the model, and the quoted excerpt must match that line. Candidates
     failing validation are rejected and counted in `stats_json` — no silent drops.
     Malformed JSON gets one strict-parse retry (precedent: claim extraction).
4. **Dedup/merge (code, not LLM).** Embed each surviving candidate; cosine-compare against
   non-terminal items of the same type (calibrated on live data: same-topic paraphrases
   0.09–0.22, distinct topics 0.38+ — threshold 0.25 sits in the gap). Within threshold →
   merge: `observation_count += 1`, append evidence, update `last_seen_at`. Merges and
   suppressions do NOT consume cap slots — only new items do. **Recently-terminal
   suppression:** also compare against items that ended (`dismissed`/`completed`/`expired`)
   within `recent_terminal_dedup_days` (default 14) — a just-cancelled plan must not be
   re-created because the topic recurred; the candidate is logged in the journal as
   suppressed. **Cap rollover:** a candidate that hits `max_new_items_per_type` is persisted
   to `dream_deferred_candidates` (full JSON) and replayed FIRST in the next run — capping
   defers, never discards (a capped candidate's evidence is already behind the session
   cursor, so it would otherwise never re-propose). Otherwise insert as new — resolutions
   as `draft` (while draft mode is on); plans as `draft`, or straight to `approved` with
   `approved_by='auto'` when `dream_config.auto_approve_plans` is on **and** the candidate's
   newest evidence citation is within `backlog_evidence_days` (7) — plans mined from older
   conversation stay `draft` even under autoplan, so run #1's backlog can never
   auto-announce something stale.
5. **Prospective pass** (`call_category='dream_prospective'`, one batched call): review
   non-terminal items created before this run —
   - *Plans:* any evidence the discussed thing happened or Bob acted — messages after the
     item's creation (including recorded announcements, findable via their
     `dream_announce` metadata marker), calendar rows, phone calls? → `completed` /
     `actioned`. **Engagement** (any linked-session reply after the last touch) resets
     the expiry clock: `expired` requires `due_hint` passed — or `plan_stale_days` with
     no due hint — **and** no engagement since the last touch. Stalled items
     (`actioned`, no progress entry in `plan_stalled_runs`) are flagged for re-raise.
     The pass may spend the plan's single follow-up re-announce (see below). A suppressed
     candidate matching a recently-terminal item whose new evidence is fresh and explicit
     (a re-commitment in a new conversation) **reopens the terminal item in place**
     (status → `approved`, evidence appended) instead of silently suppressing.
   - *Resolutions:* was the **success signal positively observed** in reviewed transcripts?
     Only positive observation marks `kept` — absence of the failing behaviour is not
     proof it was kept (there may simply have been no opportunities) and must not close a
     resolution. Not re-observed for N runs → `stale`.
   All transitions recorded with reason in `stats_json`.
6. **Journal synthesis** (`call_category='dream_synthesis'`): one narrative `journal_text`
   covering observations, creations/merges with reasons, and prospective outcomes.
7. **Announce flush.** Approved-but-unannounced plans get announced (pipeline below).
8. **Finalise.** `status='complete'`, `finished_at`, stats, journal. On exception:
   `status='failed'` with error; items already written are individually valid and stay.

Startup sweep marks `running` rows older than a timeout as `failed` (pattern of
`LLMCallStalenessTask`). The dream is read-only towards the memory system and all other
tables; it writes only `dream_*`.

## Trigger & scheduling

- Heartbeat task, self-gated on wall clock like `LocationFetchTask` (robust to interval
  changes), min interval `dream.interval_minutes` (default 240).
- **Never block the heartbeat loop.** `HeartbeatRunner` runs tasks sequentially on a 60s
  cycle; a dream run is minutes of sequential LLM calls. `DreamTask.run()` checks its
  gates, then `asyncio.create_task(...)` the run and returns immediately (established
  pattern: `routine_scheduler.py`, `email_polling_service.py`). The single-flight lock
  still guards overlap; fire-and-forget exceptions land in the run row as `failed`.
- Activity gate: run only if ≥ `min_new_sessions` (default 1) sessions qualify.
- Manual triggers: `bob dream run` (CLI) and a dashboard button — same single-flight lock,
  `trigger='manual'/'cli'`.

## Resolution quality contract

A resolution is valid only if all three hold — validated in code where possible, prompted
everywhere:

1. **Observable behaviour** — cites real messages/tool calls (`llm_call_log` failures and
   user corrections are the strongest signals; the review prompt names them).
2. **Trigger condition** — scoped (session kind, channel, activity), not global vague intent.
3. **Success signal** — checkable by a future dream. "Be more concise" ✗; "When asked a
   factual question in WhatsApp groups, answer without a tool call when the fact is in the
   prompt context — verified by reviewing subsequent group turns" ✓.

Unfalsifiable or evidence-free candidates are rejected regardless of LLM confidence.

## Autonomy tiers

- **Tier 1 (default):** in-session surfacing — when the agent dispatches in a session with
  linked non-draft plans, `prompt_assembler` injects a compact "Open plans for this session"
  block (size-capped) carrying lifecycle state: status, how long since announcement, and
  whether there was any engagement. The block's guidance lets the agent **contextually
  re-raise** a plan that has sat unactioned when the conversation invites it
  ("did you ever book that table?") — re-raising only while people are already talking is
  the primary second chance for announced plans, not a cold message. No outbound anything
  beyond that.
- **Tier 1.5 — announcement:** approved plans are announced (below). This is Bob speaking
  first in a session it already belongs to — not cold outreach.
- **Tier 2 (off by default):** *plan-initiated* outbound action — Bob acting because the
  plan says so, with no human asking in the moment (voice/WhatsApp/email outreach via
  existing dispatch paths). Requires `dream.autonomy_tier2_enabled=true` AND plan
  `status='approved'` (operator flip in dashboard). **Auto-approval never satisfies the
  Tier 2 gate** — Tier 2 always needs the operator's per-plan flip plus the global flag.
  User-instructed outbound action during a live conversation is *not* Tier 2 — it is
  ordinary tool use in a normal dispatch, available today.

## Plan approval & announcements

**Approval paths.** A plan reaches `approved` two ways: operator flip in the dashboard
(`approved_by='operator'`), or auto-approval (`approved_by='auto'`) when the runtime toggle
is on — validated plans go straight from candidate to `approved` at creation time, skipping
`draft`/`proposed`. Auto-approval is available from day one.

**`/autoplan` slash command** (in `SlashCommandsMixin` alongside `/patience`, `/verbose` —
trusted contacts only, per the existing gate at the bridge):

- `/autoplan on` — sets `dream_config.auto_approve_plans=true`; reply confirms and states
  the guardrails ("plans auto-approve and get announced here; outreach stays off").
- `/autoplan off` — back to manual approval.
- `/autoplan status` (or bare `/autoplan`) — state plus counters (pending, approved,
  announced, dismissed).

Follows the `/verbose` interaction pattern; persists to `dream_config` so it takes effect
immediately with no restart. The dashboard exposes the same toggle.

**Announcement pipeline.** When a plan becomes `approved`:

1. Collect approved plans with `announced_at IS NULL` whose **evidence session** is
   announcable (v1: WhatsApp sessions with a resolvable chat_id and active route; other
   channels rely on Tier 1 surfacing). Privacy guardrail: announce **only in the session
   where the evidence was cited** — a plan detected in a DM is never announced into a
   group, even if the group is also linked.
2. **Batch per session per flush** — several plans for the same chat become one message,
   not a burst. Respect a per-session daily announce cap (default 3) — over-cap plans wait
   for the next flush, they are not dropped. If the session is *hot* (inbound message
   within `announce_defer_active_minutes`, default 10), defer to the next flush instead of
   butting into a live conversation.
3. Compose one short, natural message per session (memory model, per D9): what Bob noticed
   was left hanging + the offer of help, referencing `due_hint` when present. Low-key,
   no confessing-about-the-dream-system mechanics — just Bob being proactive.
4. Send via `wa_bridge.send_message` (chat_id resolved with the `_session_key_to_chat_id`
   pattern from `routine_scheduler.py`), then **record the announcement ourselves**:
   `SessionService.add_message(session_key, "assistant", text, synthetic=True,
   metadata={"dream_announce": [plan_ids]})`. This is mandatory, not optional — the
   bridge send is fire-and-forget over the WebSocket and writes nothing to
   `session_messages`. `synthetic=True` stops silent-turn extraction re-ingesting Bob's
   own announcement as ground truth; the metadata marker is what the prospective pass
   searches for as evidence of action. Set `announced_at` per plan.
5. Flush triggers: end of each dream run (covers auto-approvals), the dashboard approve
   action, and a cheap `announced_at IS NULL AND status='approved'` sweep on the heartbeat
   (crash-safety; idempotent because of the guard column).

**Re-announcing (the fizzle path).** Announcements are one-shot, but a plan that lands in
silence gets exactly one follow-up, dream-decided: in the prospective pass, an unengaged
approved plan older than `reannounce_after_days` (default 3) may be re-raised once
(`max_reannounces_per_plan: 1`) into the same evidence session, gentler in tone — *"did
that dinner ever get booked?"* — recorded via `reannounced_at`. Daily cap and hot-session
defer still apply. After the follow-up is spent, the plan lives or dies by contextual
re-raise (Tier 1) and engagement-aware expiry.

**In-session plan changes & cancellation.** Participants adjust plans conversationally;
the agent does it through session-bound tools (phase 4, `services/dream/tools.py`):

- `plan_cancel(reason, plan_id?)` — status → `dismissed`, provenance appended to
  `evidence_json` (`{kind: 'cancelled', by: <contact>, at, quote}`), distinguishing
  participant cancellation from operator dismissal.
- `plan_complete(plan_id?)` — "already sorted it" → `completed`, with the person's
  statement as ground-truth evidence (this is stronger than anything a dream can infer).
- `plan_update(plan_id?, due_hint?, proposed_action?, assistance_method?, progress?)` —
  amendments in place with a `{kind: 'amended', ...}` provenance entry. Status unchanged;
  an already announced plan is not re-announced after amendment (the conversation itself
  covers it). A `progress` note records a concrete step the agent just took
  (`{kind: 'progress', by, at, note}`) and moves `approved → actioned` — live self-marking,
  so `actioned` doesn't wait for the next dream's inference (which remains the backstop).

`plan_id` is optional when exactly one open plan is linked to the session; otherwise the
agent must disambiguate first (Tier 1 injection lists them). **Enforcement:** the tools
resolve plans via `dream_item_links` restricted to the current `session_key` — a plan is
only modifiable from a session it is linked to, so participants (party to the originating
conversation) can act and outsiders cannot. Operator remains unrestricted via
dashboard/CLI. Expiry and prospective logic read the provenance entries: a cancelled plan
suppressed from re-creation still shows *why* in the journal.

**Execution model — who carries a plan out, and which turns it costs.** A plan is passive
state; it is carried by turns that happen anyway:

| Phase | Mechanism | LLM turns |
|---|---|---|
| Creation | dream review pass | dream's per-session call (luna) |
| Announcement | compose + WS send + record | 1 memory-model call, no tools |
| Waiting | "Open plans" block injected into every normal dispatch in linked sessions (`prompt_assembler`, size-capped) | 0 extra — context on existing turns |
| Live execution | person engages → normal dispatch; agent uses its **existing** tools (calendar, contacts, send) guided by `assistance_method`, self-marks via `plan_update(progress=…)` → `actioned` | 0 extra |
| Closure | `plan_complete`/`plan_cancel` inside a normal turn, or prospective pass infers (`actioned`/`completed`/`expired`) | 0 extra / dream's batched call |
| Nobody engages | contextual re-raise next time the session is alive (injection carries announce-age + engagement); dream may spend one follow-up re-announce; expiry needs due passed AND no engagement | dream's batched call, up to `interval_minutes` lag |

No plan-initiated agent loop exists — Tier 2 (off by default) would be the only source of
plan-initiated outbound turns beyond the capped follow-up above. A truly quiet session gets
one follow-up at most; anything more (routine-driven nudges) stays future work.

**Worked example — "Bob books the restaurant."** The dream (luna) creates the plan; the
announcement (luna) offers — *"Want me to book Mama San for Saturday?"* — and books nothing.
The actual work happens in the **next normal dispatch after a human consents** ("yes, 7pm"):
that dispatch runs on the default model, carries the injected plan, and executes a standard
tool loop — calendar conflict check → resolve the restaurant's number (contacts/memory, or
the user supplies it — **there is no web-search tool**) → place the call via
`create_subagent(agent_type="openai_voice", modality="phone")`, whose structured outcome
returns to the session → calendar event → confirmation → `plan_update(progress=…)` →
`actioned`. **Consent boundary:** user-instructed outbound action inside a live conversation
is ordinary tool use, available today regardless of dream flags; Tier 2 gates only
*plan-initiated* action — Bob acting because the plan says so, with no human asking in the
moment. If nobody ever replies, nothing books; the plan expires.

**Capability grounding.** `assistance_method` must be deliverable by the agent's real tool
surface. The review prompt includes a capability manifest (calendar, contacts, email send,
WhatsApp send, phone subagent dispatch, workspace/docs/skills, memory — notably **no
web search**), and candidates proposing assistance with no matching capability are
rewritten down to what is possible (offer to draft, ask the user for the number) or
rejected by validation. Plans must not promise what Bob can't do.

**Rollout sequence:**

1. *Draft mode* (`dream.draft_mode=true`, default): runs write items+journal; resolutions
   stay unsurfaced until promoted. Plans may be auto-approved from day one via `/autoplan on`
   (operator's call) — the quality checklist below is the sanity net, not a blocker.
2. Tier 1 live after promoting drafts (or immediately alongside `/autoplan on`).
3. Tier 2 only after plan precision is verified on real samples.

## Surfaces

- **Agent:** prompt-injection block (Tier 1, above) + `recall` augmentation — when a recalled
  entity has linked items, append "Open plans / Resolutions" lines (same pattern as the
  "Referenced by:" section). Session-bound plan tools (`plan_cancel` / `plan_complete` /
  `plan_update` — see "In-session plan changes & cancellation") let participants adjust
  plans conversationally.
- **Dashboard:** `/dreams` page (follows the memory pipeline tab idiom), four tabs:
  - **Journal** (default) — run list (timestamp, window, sessions reviewed,
    created/merged/expired counts, status, model, token spend) with run detail: journal
    narrative where every created/merged/suppressed/expired decision links to its item and
    evidence, and evidence citations deep-link into the session transcript. Suppressed and
    capped candidates are listed here — "no silent drops", made visible.
  - **Resolutions** — status-filtered table: behaviour/trigger/success signal, observation
    count, first/last seen, evidence links; draft rows promote/drop.
  - **Plans** — status-filtered table: proposed action + assistance method, due hint,
    announce state (`announced_at`/`reannounced_at`, engagement since), links to session
    and people/group entities; approve/dismiss actions; **draft review queue** pinned on
    top — the surface the first-week quality gate happens on.
  - **Controls** — autoplan toggle (same `dream_config` value as `/autoplan`), draft-mode
    state, effective settings (interval, caps), recent announcement log.
  Cross-links both ways: session detail pages show linked dream items; person/group memory
  entity pages show their open plans and resolutions. API under
  `/dashboard/api/dreams/*` (routers package split by domain, new `dreams.py`).
- **Slash commands (WhatsApp, trusted contacts):** `/autoplan on|off|status` — see
  "Plan approval & announcements".
- **CLI:** `bob dream run [--dry-run]`, `bob dream status`, `bob dream list [resolutions|plans]`,
  `bob dream autoplan [on|off]` (same config value, for operators not on WhatsApp).

## Cost controls

- **All dream LLM calls — review, prospective, synthesis — use `openai.get_memory_model()`**,
  the low-cost slot (currently `gpt-5.6-luna` via `BOB_OPENAI_MEMORY_MODEL` in
  `/home/bob/config/.env`; default model is `gpt-5.6-sol`). No per-pass escalation, no
  overrides. Caveat: `get_memory_model()` falls back to `default_model` when the env var is
  unset — the resolved model is recorded per run in `dream_runs.model` so any fallback is
  visible in the dashboard rather than silent.
- Per-session cap and per-run session cap; transcript length caps with pre-summaries.
- `max_new_items_per_type` (default 3) per run; excess candidates logged in the journal.
- Every LLM call goes through `LLMDispatchService` (call ids recorded in `stats_json`), so
  cost is auditable from `llm_call_log`.

## Module layout

```
services/dream/
  __init__.py
  models.py        # Pydantic models: DreamRun, Resolution, Plan, candidates
  store.py         # CRUD, lifecycle queries, link management
  runner.py        # DreamRunner: single-flight, window claim, orchestration, staleness sweep
  review.py        # retrospective pass: transcript assembly, candidate validation
  prospective.py   # prior-item review, transitions
  journal.py       # synthesis call + journal_text
  prompts.py       # review/prospective/synthesis templates
  announce.py      # approval→announcement pipeline (compose, batch, send, guard)
  config.py        # runtime config helpers over dream_config (env = boot defaults)
  tools.py         # agent-facing tools (phase 4)
heartbeat.py       # + DreamTask
routers/dashboard_api/dreams.py
cli/dream.py
schemas/35X_dream_tables.sql
```

## Implementation phases

Each phase lands independently green.

**Phase 1 — Foundations (no LLM):**
- [x] Migration: six tables (runs, resolutions, plans, links, session-review cursors, config)
      + indexes + embeddings table
- [x] `services/dream/` skeleton: models, store, lifecycle queries, link management
- [x] `DreamRunner`: single-flight lock, run-row lifecycle, window/cursor logic, staleness sweep
- [x] `DreamTask` heartbeat registration + `dream.*` settings (all defaults safe: enabled=false)
- [x] CLI `bob dream run/status` (run = window claim + empty journal, proves plumbing)
      + indexes + embeddings table
- [ ] `services/dream/` skeleton: models, store, lifecycle queries, link management
- [ ] `DreamRunner`: single-flight lock, run-row lifecycle, window/cursor logic, staleness sweep
- [ ] `DreamTask` heartbeat registration + `dream.*` settings (all defaults safe: enabled=false)
- [ ] CLI `bob dream run/status` (run = window claim + empty journal, proves plumbing)

**Phase 2 — Retrospective pipeline:**
- [x] Session collection query (window, overlap, caps, subagent exclusion)
- [x] Transcript assembly + roster/group context (reuse claim-extraction patterns)
- [x] Review call, candidate validation incl. evidence checking
- [x] Embedding dedup/merge path (cosine metric — vec0 defaults to L2, see migration 359; cap rollover via dream_deferred_candidates)
- [x] Item + link writes; stats_json; journal synthesis call

**Phase 3 — Prospective half & lifecycle:**
- [x] Prospective pass: plan/resolution status transitions with reasons
- [x] Expiry/staleness sweeps; escalation flags in journal

**Phase 4 — Surfaces & announcements:**
- [x] Dashboard API + `/dreams` page — Journal / Resolutions / Plans / Controls tabs,
      draft review queue, approve/dismiss, evidence deep-links into sessions
- [x] Announcement pipeline: approve→compose→batch→send, `announced_at` guard,
      heartbeat sweep for approved-but-unannounced plans
- [x] `/autoplan on|off|status` slash command + `bob dream autoplan` + dashboard toggle
- [x] Session-bound plan tools (`plan_cancel` / `plan_complete` / `plan_update`) with
      link-based permission enforcement
- [x] Prompt-injection block (Tier 1, gated on `draft_mode=false`)
- [x] `recall` augmentation
      draft review queue, approve/dismiss, evidence deep-links into sessions
- [ ] Announcement pipeline: approve→compose→batch→send, `announced_at` guard,
      heartbeat sweep for approved-but-unannounced plans
- [ ] `/autoplan on|off|status` slash command + `bob dream autoplan` + dashboard toggle
- [ ] Session-bound plan tools (`plan_cancel` / `plan_complete` / `plan_update`) with
      link-based permission enforcement
- [ ] Prompt-injection block (Tier 1, gated on `draft_mode=false`)
- [ ] `recall` augmentation

**Phase 5 — Rollout:**
- [ ] Enable on live data in draft mode; quality review on dashboard samples
      (resolutions stay draft until promoted)
- [ ] `/autoplan on` is available day one — operator's call when to flip it; when on,
      watch announcement quality per session for the first days
- [ ] Promote drafts; Tier 1 live
- [ ] (Later, separate decision) Tier 2 enablement

## Configuration

```python
class DreamSettings:
    enabled: bool = False
    interval_minutes: int = 240
    min_new_sessions: int = 1
    min_new_messages_per_session: int = 4
    max_sessions_per_run: int = 8
    first_run_lookback_days: int = 14
    backlog_evidence_days: int = 7         # older evidence never auto-approves
    max_new_items_per_type: int = 3
    dedup_distance_threshold: float = 0.25   # cosine; calibrated: same-topic 0.09–0.22, distinct 0.38+
    draft_mode: bool = True
    auto_approve_plans: bool = False        # boot default; runtime value lives in dream_config
    announce_daily_cap_per_session: int = 3
    announce_defer_active_minutes: int = 10
    reannounce_after_days: int = 3          # unengaged plan may get its one follow-up
    max_reannounces_per_plan: int = 1
    plan_stalled_runs: int = 2              # actioned w/o progress entries → flag for re-raise
    plan_stale_days: int = 14               # expiry horizon when no due_hint exists
    recent_terminal_dedup_days: int = 14   # cancelled/completed plans block re-creation
    autonomy_tier2_enabled: bool = False
    resolution_kept_consecutive_runs: int = 3      # K consecutive positive observations → kept
    resolution_stale_runs: int = 5                 # N
```

## Testing & verification

- Unit: store lifecycle, per-session cursor maths (first-sight lookback bound, failed-run
  recovery, cap overflow carry-over), evidence validator (index exists + excerpt matches;
  fabricated indices rejected), dedup merge semantics; announcement batching (one message
  per session per flush), daily cap deferral (not drop), hot-session deferral, DM-privacy
  guardrail (evidence-session-only), no-double-announce via `announced_at`, non-WhatsApp
  sessions skipped without error; announcement recording writes `synthetic=1` +
  `dream_announce` metadata (future dreams can find it; extraction skips it).
- Heartbeat non-blocking: `DreamTask.run()` returns before the run completes — assert the
  heartbeat cycle proceeds while a (stubbed, slow) dream is in flight.
- Plan tools: session-bound enforcement (plan visible/modifiable only from a linked
  session; unlinked session gets empty list and rejection with reason), cancel/complete
  provenance entries written, field amendment leaves status untouched while `progress`
  moves `approved → actioned`, and recently-terminal suppression blocks re-creation
  within the window (and not after it).
- First run: lookback bound holds (sessions with only older activity never qualify),
  newest-first ordering fills the cap with the freshest sessions, and the backlog guard
  keeps autoplan from approving plans whose evidence predates `backlog_evidence_days`.
- Fizzle path: second re-announce attempt rejected (`max_reannounces_per_plan`), a reply
  after announcement resets the expiry clock (engaged plan with passed `due_hint` is NOT
  expired), stalled detection flags `actioned` items without progress entries, and a
  fresh-explicit-evidence candidate reopens a terminal item in place rather than
  suppressing.
- Integration: `DreamRunner` end-to-end with a stubbed `LLMDispatchService` returning fixed
  candidates — asserts items, links, stats, journal, and re-run idempotency via dedup;
  approval→announce flush is idempotent across crash-replay (sweep finds nothing to resend).
- Smoke: `bob dream run` against the live DB in draft mode (nothing surfaced); inspect
  `/dreams` page; confirm `llm_call_log` categories and token spend.
- Quality gate before Tier 1: sample ≥20 drafted items; check groundedness (citation resolves),
  dedup correctness (no near-duplicates open), plan sensibility (would surfacing it be fine?).
  With `/autoplan on`, additionally review the first days of announcements per session — tone,
  batching, and that nothing announced reads as intrusive.

## Open questions (decide during implementation)

- Should plan detection also read voice-call structured outcomes (`phone_calls`) as evidence,
  or transcripts only for v1?
- Luna's precision on implicit-commitment detection is the main quality risk. Evidence
  validation + caps + `/autoplan off` are the safety nets; decide from draft-mode data
  whether auto-announcement additionally needs an evidence-strength bar (e.g. ≥2 cited
  messages or an explicit commitment phrase).
- Resolution scoping: global vs per-channel/persona semantics once links exist — start global
  with links as metadata only.
- Deep overnight dream (longer window, program-derivation-friendly) vs uniform interval —
  revisit after phase 5.
