# Bob Events Plan v2 — Multi-Party Event Orchestration

**Objective:** take Bob from his current state to an agent with very high odds of
successfully planning a lunch for the AI-doom team end to end: negotiating times,
finding and finalizing a venue, calling to book, reminding people, and producing
official t-shirt merchandise.

**Benchmark scenario (acceptance test):** "Bob, organise a team lunch for the
AI-doom group in the next two weeks" → a booked restaurant, a confirmed attendee
list, reminders sent, and approved t-shirts ordered — with zero information lost
when attendees reply in the "wrong" channel.

> **v2:** revised after external review. Headline changes: an explicit
> cross-conversation entity-identity mechanism (§2.0 — the real crux of G4),
> a durable inline router instead of event-bus subscription (§2.2–2.3), a
> specified tool-surface and injection-site inventory (§1.5), an explicit wake
> matrix (§1.2), a full `revise_goal_state` contract with a no-autonomous-
> actuation rule (§1.3), and a properly scoped rehearsal harness (§4.3).
> Factual corrections: `session_messages` → `messages` (migration 457); the
> DM outreach tool is `send_whatsapp_to_contact` (DM-only by design); image
> generation is the `skills/openai-image` workspace skill, not
> `openai_service`; next migration number is **459**; dream-reconciliation is
> a heartbeat task, and dream scheduling is gated off by default (§4.1).

---

## 0. Current-state audit (what this plan builds on)

### Memory (v7, post-bulletin)
> ⚠️ The *bulletin* pipeline is **retired**. Migration
> `353_drop_bulletin_dream_tables.sql` dropped `memory_bulletins` and friends
> and removed `memory_claims.source_bulletins`. References in
> `services/memory/models.py` (`Bulletin`, `BulletinGeneratorInput`) and parts
> of `service.py` are legacy residue (cleanup folded in below).
>
> The **live path is silent-turn extraction**: when a session has messages
> newer than the last silent turn, an idle extractor turn runs with a narrow
> write-oriented tool subset (`extraction_tools.py` — `add_claim` /
> `create_entity`, no retract/supersede). Every claim records the extracting
> turn's message id in `source_messages`. Extraction is additive;
> reconciliation (throttled heartbeat task, ~hourly) repairs state separately.

Key memory facts:
- Claims are the source of truth; entities are identity records; rendered views
  come from `claim_types.py` templates. `event`, `task`, `group`, person types
  exist (event = start_time + attendees + location).
- **Entity ids are model-chosen slugs at extraction time** — `create_entity`
  takes the id verbatim; no alias table, no write-time embedding check;
  FTS/embedding matching only catches up at reconciliation. This is the
  identity-fragmentation risk §2.0 addresses.
- `write_claim` is called from extraction, `memory_correct`, reconciliation,
  and question-answering; the dedupe path widens an existing claim's
  `source_messages` without inserting a row.

### Goals (Bob3 Phase V)
- `goals` (`420_bob3_goals.sql`): `conversation_id` (working session_key),
  `origin_conversation_id` (woken on settle), `kind`, `objective`,
  `strategy_json`, `progress`, `result`; CAS on status for transitions and on
  `version` for revisions; `goal_transitions` audit.
- `settle_goal` cancels wakeups, appends a goal event, wakes the origin.
  **Multiple callers** settle goals: goal tools, `finish_outreach`, subagent
  service, `phone_call_result_service` (which does its own origin wake), and
  dashboard cancel — so roll-up semantics must live in `settle_goal` itself.
- No hierarchy, no goal↔conversation set; only outreach goals are injected
  into prompts; goal tools are wired only into the trusted WhatsApp inbound
  path; `create_goal` (tool) exposes no kind/origin/strategy; `update_goal`
  can't touch `strategy_json`.
- Conversations proper exist since Phase VI increments: `messages` is keyed by
  canonical conversation id; `ConversationRepository.resolve_cid(session_key)`
  maps endpoints to conversations (bindings can merge).

### Channels & actuators
- WhatsApp: `send_whatsapp_to_contact` creates per-target DM outreach goals
  with 24h deadline wakeups. **DM-only by design** — group tools are read-only;
  no proactive group-send tool exists (bridge-level send does).
- Voice: `create_subagent(agent_type="openai_voice", modality="phone")`;
  realtime tools are `end_call`/`report_success`/`report_failure` with a
  free-text outcome — **the call subagent has no memory tools**.
- No LLM tool schedules a wakeup or routine directly.
- Effects system: durable idempotent executors, **one global unique key
  string** per effect.
- `approvals` table: created by migration 160, **dropped as dead by 458**
  (no code users; rows archived) — Bob Events migration 460 recreates it
  fresh with the `purchase` approval type.
- Image generation: `skills/openai-image` workspace skill (writes files;
  WhatsApp media-send handles files fine).

### Gap summary
| # | Gap | Consequence for the lunch |
|---|-----|---------------------------|
| G1 | No goal hierarchy | No roll-up; every child wakes the origin |
| G2 | No structured reasoning state on goals | State re-derived from transcripts every wake |
| G3 | Goals invisible in prompts (except outreach) | LLM must poll `list_goals` |
| G4 | Memory claims don't route to goals — **and entity ids fragment across conversations** | Group-chat replies never reach the DM-born plan |
| G5 | No entity↔conversation index | "Who knows about E?" requires a scan |
| G6 | No progress-review loop | Stuck goals rot until deadline |
| G7 | Tool surface gaps | No group send, no goal-state editing, no wakeup tool, no merch/ordering |

---

## Phase 1 — Goal hierarchy & reasoning state (foundation)

### 1.1 Migration 459: hierarchy + holders
```sql
ALTER TABLE goals ADD COLUMN parent_goal_id TEXT REFERENCES goals(id);
CREATE INDEX idx_goals_parent ON goals (parent_goal_id, status);

CREATE TABLE goal_conversations (
    goal_id TEXT NOT NULL REFERENCES goals(id),
    conversation_id TEXT NOT NULL,       -- canonical cid via resolve_cid()
    role TEXT NOT NULL DEFAULT 'holder', -- holder|origin|worker
    created_at TEXT NOT NULL,
    PRIMARY KEY (goal_id, conversation_id)
);
CREATE INDEX idx_goal_conversations_cid ON goal_conversations (conversation_id);
```
- **Store canonical cids, not raw session_keys** (`resolve_cid` at insert) —
  bindings can merge, and §2.3 echo-suppression breaks if origin and holder are
  the same conversation under different keys.
- Role semantics: `origin` = asker; `worker` = auto-registered subagent/outreach
  sessions doing the work; `holder` = any conversation granted visibility
  (e.g. the group chat for a lunch plan). `create_goal` registers
  conversation_id as worker and origin_conversation_id as origin.
- Lifecycle: rows are kept on settle (audit value, small volume) but excluded
  from prompt-injection queries once the goal is terminal; a cleanup sweep
  deletes rows for goals terminal > 90 days.
- Repo additions: `children_of`, `add_holder`, `holders_of`,
  `goals_held_by(cid)`.
- In passing: fix `overdue()` comparing second-truncated timestamps against
  microsecond-ISO writers (directly coupled — deadline wakeups drive §3).

### 1.2 Roll-up semantics and the wake matrix
The parent-update hook lives **inside `settle_goal`** (single chokepoint — all
five caller paths inherit it). `phone_call_result_service`'s independent origin
wake is folded into this path.

Explicit wake matrix:

| Stimulus | Parent goal update | May wake working conv | May wake origin conv | call_category |
|---|---|---|---|---|
| Child settle (parent exists) | reviser runs on parent | yes, if `wake_needed` | no | `goal_progress` |
| Root goal settle | — | n/a | yes (today's behaviour) | `goal_result` |
| Silent revise (routed claims) | reviser runs | only if `next_actions` changed | no | `goal_progress` |
| Deadline wakeup | reviser runs first | yes — wake targets the **root goal's working conversation**, never an outreach child's target DM | no | `goal_deadline` |
| Reviser escalation (stuck, §4.1) | — | yes | yes, if working conv stalled twice | `goal_escalation` |

What §1.2 prohibits, precisely: a **child** settle never *directly* wakes the
root origin; information flows child → parent state → (reviser-judged) wake of
the parent's working conversation. The origin hears from the root goal only.
Child deadline wakeups re-target: a child's deadline wakes the parent chain's
working conversation. (Today the deadline lands on
`origin_conversation_id or conversation_id` — for an outreach child that is the
requester, for a subagent child the parent: the right target by coincidence.
The hierarchy makes the target structural rather than accidental, and keeps it
correct for future child kinds that set neither field usefully.)

### 1.3 The `revise_goal_state` contract
`strategy_json` schema (versioned: `{"v": 2, ...}`):
```json
{
  "v": 2,
  "plan": "one-paragraph current strategy",
  "known": ["8 invitees; 5 confirmed (alice, ...)"],
  "open_questions": ["carol & dan availability"],
  "next_actions": [{"action": "chase carol", "due": "2026-08-26T10:00Z"}],
  "refs": {"entities": ["event-team-lunch", "group-ai-doom"], "claims": ["..."]}
}
```
New `services/goal_state_service.py`, `revise_goal_state(ctx, goal_id, stimulus)`:
- **Model:** configurable `settings.goals.reviser_model`, following the
  `settings.patience.model` / recon-override precedent; default a cheap model.
- **Legacy migration:** on first revise of a goal whose `strategy_json` lacks
  `"v"`, wrap it: outreach-shaped `{requestor, message}` moves under a
  `legacy_outreach` key inside the v2 envelope; nothing rewrites goals that are
  never revised.
- **Validation:** reviser output is schema-validated (pydantic). Malformed →
  one retry with the validation error in-prompt; still malformed → **keep old
  state, log, and set `wake_needed=true`** with the raw stimulus as summary
  (degrade to "tell the main model" rather than lose information).
- **CAS retry:** read → revise → `revise(expected_version)`; on conflict
  re-read/retry ×3. Exhaustion → same degrade-to-wake fallback. (Concurrency
  is also bounded upstream by per-goal serialization, §2.3.)
- **stimulus_id:** claim batch → the extraction turn's message id;
  child settle → `settle:{child_goal_id}:{to_status}`; deadline →
  `wakeup:{wakeup_id}`. **Idempotency key format:**
  `goal_revise:{goal_id}:{stimulus_id}` (one global effects key string).
- **Hard rule — no autonomous actuation:** the reviser updates state and may
  set `wake_needed`. It may **not** create goals, place calls, send messages,
  or schedule wakeups. Anything requiring action (e.g. §3.3's "call the
  restaurant back") becomes a `next_actions` entry + wake; the woken working
  conversation's main model — with its full tool surface and judgment —
  decides. This keeps every actuator behind a capable model.

### 1.4 Goal-context injection — named sites, not "every session"
There is no single assembly chokepoint. The goal block (a new
`context_assembler.goals_block(cid)`) is added at exactly these sites:
1. WhatsApp inbound composition (`whatsapp_bridge_service/_service.py` ~826—
   831) — replaces the special-cased `active_outreach` block (outreach's
   requestor/message ride in `legacy_outreach`/`plan`).
2. The generic wake dispatch path (`wake_service._generic_wake_dispatch`) —
   critical: this is what handles `goal_deadline`/`goal_progress` wakes.
3. Email reply composition in the email polling/agenda path.
Routines stay self-contained (a routine that needs goal context can hold the
goal). Budget: top **5** active goals by `updated_at` per conversation,
each rendered ≤ ~150 tokens (plan truncated, known/open capped at 5 items) —
bounded, unlike today's assembler blocks.

### 1.5 Tool-surface extensions (closes G7's non-merch half)
| Tool | Change | Exposure |
|---|---|---|
| `create_goal` | add `kind`, `parent_goal_id`, `strategy` (validated v2 subset), `deadline` | trusted paths |
| `update_goal_state` | new: read-modify-write on own goal's strategy via CAS (same validation as reviser) | trusted paths |
| `list_goals` | include children + state summary | all paths with goal tools |
| `send_whatsapp_group_message` | **new** proactive group send | see policy below |
| `schedule_goal_wakeup` | new: wakeup against a goal at an ISO time (reminders) | trusted paths |
| `respond_approval` | new: approve/reject a pending `approvals` row (effect-guarded; the §3.4 POD order executor's precondition checks `approvals.status='approved'`) | conversation holding the pending approval (the origin) |
| goal tools generally | wire into the **generic wake path** and email path (today: WhatsApp inbound only — a `goal_deadline` wake currently tells the LLM to "revise the goal" while giving it only workspace tools) | — |

Group-send policy (trust/abuse): only into groups where Bob is already a
member; rate-limited per group per hour; every send records `goal_id`
provenance; disabled by default per group until allowed via
`conversation_policy` (migration 452 machinery). Reminder scheduling (§3.3) is
expressed as **child goals with deadlines** — no separate routine tool needed.

**Tests:** repo CRUD + CAS-retry; settle roll-up via *each* settle caller
(tool, finish_outreach, subagent, phone-result, dashboard); wake-matrix
integration test; prompt-injection snapshot with budget assertions; legacy
strategy migration test.

---

## Phase 2 — Memory→goal routing

Design principle: **route structurally, probe secondarily** — but structural
matching is only as good as entity identity, so that comes first.

### 2.0 Cross-conversation entity identity (the crux of G4)
Nothing today forces the group-chat extractor to reuse `event-team-lunch`
rather than minting `event-lunch`; a fresh slug empties both the refs
intersection *and* the mentions index (keyed by the same wrong id). Three
mechanisms, layered:

1. **Goal-aware extraction seeding (primary).** The silent-turn extractor
   prompt for a conversation gains a *candidate entities* block: the union of
   `refs.entities` from active goals held by that conversation
   (`goal_conversations`) **plus** goals held by conversations sharing
   participants (roster overlap via participants tables from migration 456),
   rendered with display names + one-line purpose. Extraction rule added:
   "reuse a candidate entity id when the conversation refers to the same
   real-world thing; do not mint a near-duplicate."
2. **Write-time soft resolution (backstop).** `create_entity` in the extractor
   toolset checks new ids against candidates + FTS on display_name; on a
   strong match it returns "use `event-team-lunch` instead" to the extractor
   rather than silently creating. (Reconciliation's merge machinery remains
   the eventual repair for what slips through.)
3. **Weak-match routing (safety net, §2.3).** Claims about *unmatched new*
   entities still reach goals via participant/conversation overlap + probe —
   defined as a first-class path, not an afterthought, because 1–2 will miss.

**Test (revised per review):** the §2.4 integration test must include the
"new slug in the wrong channel" case — extractor deliberately mints
`event-lunch` in the group chat; assert the claim still reaches the
`event-team-lunch` goal via layer 3, and that reconciliation later merges the
entities.

### 2.1 Conversation-interval index (migration 459, same file)
```sql
CREATE TABLE memory_entity_mentions (
    entity_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,       -- canonical cid
    first_message_id TEXT NOT NULL,
    last_message_id TEXT NOT NULL,
    first_at TEXT NOT NULL,
    last_at TEXT NOT NULL,
    PRIMARY KEY (entity_id, conversation_id)
);
```
Derivation: claim `source_messages` → **`messages`** rows (migration 457;
*not* the dropped `session_messages`) → `conversation_id` directly.
**Maintained in `write_claim` itself** (single chokepoint), so extraction,
`memory_correct`, reconciliation, and answer-question all update it; the
dedupe path that widens `source_messages` without a new row also upserts the
interval. Backfill script walks existing claims.

### 2.2 Router trigger — inline + watermark, not event-bus
The in-process `event_bus` is volatile and drop-on-overflow (dashboard-only
today) — unsuitable as the router's trigger. Instead:
- **Inline:** the silent-turn extraction post-loop (`service.py` ~420–454,
  which already re-derives the turn's new claims) calls the router directly
  with `{conversation_id, claim_ids, entity_ids, turn_message_id}` after
  committing. One batch per extraction turn (supersede/merge churn never
  re-fires — those paths don't route; see §2.3 scope).
- **Durability:** the same payload is appended to `event_log` as
  `memory.claims_created` *before* routing. A startup/heartbeat sweep replays
  events newer than a `claim_router_watermark` (single-row table), advancing
  the watermark only after the routing effect is durably enqueued — a crash
  between extraction and routing is replayed, not lost.
- **Concurrency/backpressure:** revisions are effects executed on a per-goal
  serial queue (effects executor keyed `goal_revise:{goal_id}:*` runs one at a
  time per goal); router matching itself is pure SQL and fast. Global cap on
  concurrent reviser LLM calls (semaphore, default 3).
- `event_bus.publish` still fires for the dashboard feed — as telemetry, not
  trigger.

**Routing scope (explicit):** only extraction batches route.
`memory_correct` and reconciliation supersedes do not — corrections come from
a user actively talking to Bob (the conversation handles it), and recon
repairs state rather than adding information. Revisit if rehearsal shows gaps.

### 2.3 Router (`services/memory/claim_router.py`)
1. **Echo suppression:** drop the originating cid from candidates (canonical
   cids make this reliable across merged bindings).
2. **Structural match (no LLM):** active goals where `refs.entities` ∩ event
   `entity_ids` ≠ ∅, or held (via `goal_conversations`) by a conversation in
   `memory_entity_mentions` for those entities.
3. **Weak match:** goals held by conversations with participant overlap with
   the originating conversation (§2.0 layer 3). Overlap is computed on
   `participants.contact_id` (never the raw identifier string), and the
   agent's own participant rows are excluded — Bob is a participant of
   effectively every conversation, so including him would weak-match every
   goal to every conversation and push all filtering onto the probe.
4. **Relevance gate:** weak matches only pass through a cheap probe ("goal
   objective + state + new claims → RELEVANT / IGNORE"). Direct ref matches
   skip it. **Fails open to RELEVANT** (probe error/timeout ⇒ deliver), same
   asymmetry rationale as Tier 2 failing open to ACT: a wrong IGNORE loses
   information silently. Note: this is *not* a plug-in to the attention
   coordinator (Tier 2 is one hardcoded prompt, not a registry) — it is simply
   another cheap-model call owned by the router.
5. **Delivery:** `revise_goal_state` per matched goal (§1.3 contract). The
   reviser sees **conflicting active claims as normal input** — at route time
   both "attending" (old) and "can't make it" (new) are active, since
   supersession happens at reconciliation up to ~1h later; newest-claim-wins
   guidance is in the reviser prompt.
6. **Log:** every decision to `memory_routing_log` (its own table, migration
   459 — analogous in spirit to `attention_shadow`, not stored in it):
   claim_ids, candidate goal, match type (ref/mention/participant), probe
   verdict, revise outcome, wake decision.

**Tests:** the motivating scenario — attendance stated in the *group chat*
updates a DM-born lunch goal with no wake when already known, a wake when
`next_actions` change; the §2.0 wrong-slug case; watermark replay after a
simulated crash; conflicting-actives revision.

---

## Phase 3 — Scenario capabilities

### 3.1 Time negotiation (`kind="negotiate"`)
- Root `plan-team-lunch` (kind `task`, template §3.5); child `negotiate-time`.
- Fan-out: `send_whatsapp_to_contact` per invitee (DM outreach goals parented
  under `negotiate-time`); roster from `group-ai-doom` member claims. The
  group chat is registered as a *holder* of the root goal, so §2.0 seeding
  covers replies given there.
- Group poll messages use the new `send_whatsapp_group_message` (§1.5) — this
  did not exist as an LLM capability before.
- **Decision rule** in strategy: `{"quorum": 0.75, "of": "invitees",
  "decide_by": "<iso>"}`. Quorum denominator is **invitees**, not responders
  (prevents deciding on 2/2 replies). If `decide_by` fires below quorum, the
  deadline wake tells the working conversation to decide with what it has —
  responders-only majority, non-responders noted in `known`; the main model
  may equally choose to extend once. Quorum reached early settles the child.

### 3.2 Venue + booking
- Child `find-venue`: research subagent → candidates into goal state; dietary
  claims constrain the shortlist; finalists polled in the group chat.
- Child `book-venue` (kind `call`): `openai_voice` phone subagent with
  slot/headcount in-instruction. **Dataflow after the call (explicit):** the
  call subagent has no memory tools — it returns a free-text outcome via
  `report_success`. Settle → parent reviser → wake of the working
  conversation, whose main model (with memory tools) parses the outcome and
  writes the `event-team-lunch` **event entity** + attendance claims, making
  the booking routable memory. The entity id is then in the root goal's
  `refs`, seeding §2.0 everywhere.
- **Named risk:** restaurants are frequently landlines; the known
  echo/self-barge-in failure profile (Broken Hill Hotel case) is exactly this
  call type. Rehearsal must include an echo-prone simulated callee; if
  unresolved, fall back to the goal noting "phone unreliable → try
  booking-site/email child."

### 3.3 Reminders
- Child `remind-attendees` with **deadline child goals** at T-24h/T-2h from
  the event entity's `start_time` (via `schedule_goal_wakeup`, §1.5) — wakes
  the working conversation, whose main model sends group + DM reminders.
- Cancellations close the loop with **no new mechanism**: "can't make it"
  said anywhere → extraction → router → reviser updates headcount → wake
  with `next_actions: ["call restaurant re headcount"]` → main model spawns a
  new `call` child. (Per §1.3, the reviser itself never places the call.)

### 3.4 T-shirt merchandise
- Child `produce-merch`:
  1. Design via the `skills/openai-image` workspace skill (writes files;
     media-send delivers them).
  2. Approval: designs posted to the group (new group-send tool); reply
     preferences arrive as claims → router.
  3. Sizes collected in the §3.1 fan-out ("also, t-shirt size?").
  4. Order via print-on-demand REST (Printful/Printify) as an **effect
     executor**; phone/email fallback.
- **Payment gate mechanics:**
  - Draft cart persisted in the goal state (`legacy`-style sub-key
    `pending_order`), plus an `approvals` row — **reuse migration 160's table**,
    extending the `approval_type` CHECK with `'purchase'` (new migration).
  - Approval = affirmative reply in the **root goal's origin conversation**
    (wherever the ask came from — WhatsApp or dashboard; the dashboard
    approvals UI already reads this table). Recorded durably by flipping the
    approvals row via the `respond_approval` tool (§1.5); the order effect's
    precondition checks `approvals.status='approved'`.
  - Timeout: 72h deadline child → wake origin once → then mark the approval
    `cancelled` and the merch child `failed` (never auto-approve).
  - **Credentials:** POD API keys live in server-side config read only by the
    effect executor — never in any environment reachable from agent bash
    (same class as the open Twilio-creds finding).
- **Privacy rule for routed personal data:** sizes/addresses/dietary claims
  are person-subject claims; the router delivers them to goal *state*, but the
  reviser prompt carries a visibility rule: facts sourced from a DM
  (`memory_entity_mentions` origin is a DM cid) are marked `private_source` in
  `known` and the main model must not repeat them into group sends (sizes go
  to the vendor order, not the group poll; dietary needs surface as "2
  vegetarian" aggregates, not names). Claim-level `visibility` derivation
  (DM ⇒ `contact`) is added to the extractor rules in the same change.

### 3.5 Playbook, not hardcode
Goal template `kind="event_plan"`: default child DAG (negotiate → venue →
book → remind ∥ merch) + decision rules as strategy data. The LLM instantiates
and adapts via the extended goal tools (§1.5 — now actually expressive enough:
`kind`, `parent_goal_id`, `strategy` are settable).

---

## Phase 4 — Robustness & review

### 4.1 Progress-review loop (G6)
**Not** on dream scheduling — dream is gated on `dream.enabled` (default
false; dream-v2 Phase 5 live-enablement isn't done), so the loop would
silently never run. Instead: an own heartbeat task on the
`MemoryReconciliationTask` pattern, scanning active goals with `updated_at`
older than a threshold (default 24h), running the reviser with a
"coherence-check" stimulus (`stimulus_id = review:{goal_id}:{date}`), and
escalating per the wake matrix (working conv; origin after two stuck cycles).

### 4.2 Observability
- Dashboard: goal tree (parent/child, state JSON, transitions);
  `memory_routing_log` browser.
- Kill switches: `BOB_CLAIM_ROUTER_DISABLED=1` (extraction unaffected;
  watermark holds, so re-enabling replays the gap);
  `BOB_GOAL_STATE_SHADOW=1` — revisions run, wakes suppressed **and recorded**
  in `memory_routing_log.wake_decision='shadow_wake'`.
- **Shadow evaluation baseline (what "good" is judged against):** during
  burn-in, for each `shadow_wake` we check whether the same information later
  reached the goal's working conversation by today's channels (child settle,
  deadline, user repetition) within 24h — if yes, the wake was *correct and
  earlier*; if the info never arrived, it's a caught information-loss; if the
  state diff was empty, it's noise. Enable live wakes when noise rate < 10%
  over a week of real traffic.

### 4.3 Multi-party rehearsal harness (largest unbuilt item — scoped)
Components:
- **Fake parties:** persona-scripted participants injected at payload level
  through the real WhatsApp inbound path (extending the
  `test_whatsapp_inbound_gate.py` fake-inbound approach), each with a DM
  binding + shared group; a scripted driver advances personas on Bob's
  outbound messages (captured via a stub bridge transport).
- **Clock:** no injectable clock exists (`services.base.utcnow` +
  `time.monotonic`, wall-clock wakeups). Approach: **compressed deadlines**
  (scenario runs with minutes-scale deadlines/quorum windows) for the e2e run,
  plus a `utcnow` seam introduced in `services/base.py` (single function —
  cheap) so unit-level wakeup tests can time-travel. Full fake-clock
  event-loop control is explicitly out of scope.
- **Voice leg:** the `/voice/realtime` harness expects a human; rehearsal
  instead runs the realtime bridge against a **scripted text-mode callee**
  (the bridge is audio-source-agnostic; a text transport stub plays the
  restaurant, including an echo/self-barge-in variant for the §3.2 risk).
  One manual human-voiced run remains a release checklist item.
- **POD stub:** local FastAPI stub implementing the 4 Printful endpoints the
  executor uses (product, mockup, order create, order confirm), asserting the
  approval precondition.
- **Information-loss incident (formal):** a scripted fact F (ground truth is
  known — personas are scripted) that should alter goal state, where no goal
  revision reflecting F occurs within 15 minutes (compressed-time) of F's
  injection, *and* the final state omits F. Gate: **zero** incidents across
  the scenario + 3 perturbation reruns (shuffled reply timing/channels).
- Score also: unnecessary wakes (revision with empty diff that woke), human
  interventions (driver had to go off-script to unstick Bob).

> **Harness status (2026-08-25):** implemented in
> `packages/bob-server/tests/rehearsal/` (scripted actors at the
> chat/chat_with_tools level driving the real pipeline; persona driver with
> wrong-channel plans; in-process POD stub via httpx ASGITransport, also
> runnable standalone via `python -m tests.rehearsal.pod_stub`; compressed
> deadlines; the formal zero-loss scorer). The deterministic gate
> (`all_group` — every reply in the wrong channel) passes end to end:
> zero loss, one gated order, reminders fired. The three replay variants
> are xfailed on a negotiate-settle race across detached dispatch
> generations under the scripted actor — the zero-loss metric passes
> whenever a run completes. Building the harness found five real
> production bugs (template children lacking v2 refs → fragmented routing;
> parented outreach not inheriting refs → wrong-slug DM extraction; wake
> dispatch tasks garbage-collectable mid-flight; re-entrant inline
> settle→roll-up→revise→wake chains — roll-ups now queue for the pump;
> loop-bound asyncio primitives). The voice leg is scripted at the
> call-result layer; the audio-level text-mode callee (echo variant) and
> one manual human-voiced run remain release-checklist items, and the
> authoritative rollout gate remains the live rehearsal with real models.

---

## Cleanups folded in (directly coupled)
- Delete dead `Bulletin`/`BulletinGeneratorInput` models and
  `memory_entity_bulletins` write paths in `services/memory/service.py`
  (tables dropped — latent runtime errors).
- Fold `phone_call_result_service`'s independent origin wake into the settle
  chokepoint (§1.2).
- Fix `overdue()` timestamp-format comparison (§1.1).
- Update `docs/memory.md` references to `session_messages` → `messages`.

## Sequencing & effort
| Phase | Depends on | Estimate |
|-------|-----------|----------|
| 1 Hierarchy, state, wake matrix, tool surface | — | ~1.5 weeks |
| 2 Entity identity + durable routing | 1 | ~1.5 weeks |
| 3 Scenario capabilities (incl. group-send, approvals ext., POD executor) | 1, 2 | ~2 weeks |
| 4 Review loop + rehearsal harness | 1–3 | ~2 weeks |

(Phase 4 doubled from v1 per review — the harness is the largest unbuilt item.)

**Highest-leverage items:** §2.0 (entity identity — without it routing is
theatre), §2.2 (durable trigger — without it routing is lossy), §1.4/§1.5
(the LLM can actually see and act on goal state where wakes land).
