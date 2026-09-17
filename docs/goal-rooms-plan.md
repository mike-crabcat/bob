# Goal Rooms — every goal gets its own conversation

Status: **implemented** (2026-09-16, all phases, live by default — kill switch
`BOB_GOAL_ROOMS=off`). Origin: Mike's ask after the 2026-09-16 reviser
overhaul — "the goal system isn't working well". Extends
[utility-conversations-plan.md](utility-conversations-plan.md) and
[stimulus-spine-plan.md](stimulus-spine-plan.md); retires most of the goal
reviser path and the claim router's matching/probe half.

## The problem

The goal system is now made of guards. The incident record, all within five weeks:

- 2026-09-11 — a bare `deadline` value wedged the whole due-action pump for 24h.
- 2026-09-16 — three WFH-roster goals stalled invisibly; fix required dict-op
  normalisation, deadline-wake retargeting, a creation-time seed action, and
  review-loop special-casing (the overhaul that prompted this plan).
- Reviser output fails JSON validation often enough to need a named
  degrade-to-wake path; it once emitted dict ops where lists were expected.
- Goals completed on lies: Meshy `exit 0` on insufficient credits (error
  sentinel bolted on afterwards), phantom-ack sells that never existed.
- Claim routing fails *open* and pays an LLM probe per weak candidate.

Common root: **a goal has no place to think.** The worksheet (`strategy_json`)
is a blob; the reviser sees the blob plus one stimulus, no history; deliberation
happens wherever it lands — 654 of 775 historical goals were worked inside human
group chats, the wrong audience and a scrolling context. Four drivers (due pump,
review loop, outreach probes, child rollups) poke the blob through different
doors, one of which can wedge the others.

Spend confirms the shape: last 14 days `goal_revise` ran 1,133 times (19.0M
tokens, ~81/day) and `claim_router_probe` 1,268 times (~91/day) — ~170
middleware turns/day to move information *into* goals — while the database
holds **one active goal**.

## Goal

Each goal gets a **room**: a headless utility conversation with a charter, its
own history as working memory, sensation routes feeding it world news, and
child rooms for subgoals that earn them. The goals table is demoted to a
lifecycle registry. A stalled goal becomes a question with an answer ("your
check-in woke you; what does your state block say?"), not archaeology.

## Non-goals

- **New authority.** Rooms change *where goal deliberation happens*, not what
  Bob may do. Every outbound gate (approvals, cross-group send, outreach tools)
  applies at act time exactly as today. Room creation is not approval-gated —
  approval already lives at the act boundary.
- **Replacing the memory system.** Memory remains the world model and the
  living truth; a room is a consumer of it. Post-completion drift updates
  memory, never a closed goal.
- **A task execution engine.** Rooms execute nothing new — they still carry
  work through existing tools in their own turns.
- **Deleting the reviser on day one.** It stays as the fallback path and serves
  legacy goals until they close (see Rollout).

## Settled design decisions

| # | Decision | Why |
|---|---|---|
| D1 | A goal's working conversation is a **utility conversation** (`agent:goal-<slug>:utility`), not a human chat | Charter injection, cheap model, `report_to`, valves, and the generic wake dispatch path all exist and are proven (frigate-watch). Deliberation stops polluting group chats (654 historical goals); humans receive reports, not process. |
| D2 | Working memory is the **room's history** plus a **state block** the room writes itself via a `goal_state` tool call each turn | The reviser's failure class (blind JSON patching, dict-ops, unterminated output) is deleted, not retried. The writer has full context by construction. `strategy_json` becomes a render of the state block — readers (dashboard, prompts) unchanged. |
| D3 | Memory updates arrive as **claim sensations** through the stimulus spine, not via claim-router candidate matching + probe | `claim.write.<entity_id>` events, routed by the room's own subscriptions. Delivery is per-entity and inspectable ("why did it wake?" → its routes). The probe class dies: subscription is the relevance decision, made once with context instead of per event with none. |
| D4 | **Corrections wake, adds digest**: supersession/correction claims emit `level=action`; routine new claims emit `level=info` and ride a "claims since your last turn" appendix on the next room turn | Most claims are adds; most adds change nothing. This is what actually captures the bulk of the 81 reviser-calls/day. The router's existing "info never wakes" rule does the work. |
| D5 | One coalesced event per **entity per turn** (dedup `turn_id + entity_id`), carrying the claim summaries | A busy meeting about the venue is one wake, not N — matches the DEFER+coalesce valve semantics fixed 2026-09-14. |
| D6 | **Self-echo suppression is absolute**: claims originating from a room's own conversation never route back to it, and room turns are extraction-exempt (`synthetic` marker, the dream-announce pattern) | 2026-09-16 evidence: David's WFH schedule was claim-written 4× in 4.5h, values near-identical — Bob's own roster narration re-extracted. Without this rule rooms churn worse than the reviser. Precedent: dream journals are not bulletins (dream D8). |
| D7 | The room manages **what** it listens to; the platform owns **how much** | Routes (source/type patterns within a source allowlist) are self-managed; valves stay router-enforced with hard ceilings (`MIN_COOLDOWN_S`, `MAX_BUDGET_PER_HOUR`). Over-subscription costs cheap tokens in the room's own turns; authority is unchanged. Tightening autonomous, loosening bounded by ceiling — never past it. |
| D8 | **Generous seed, deliberate pruning**: initial routes cover every entity linked at creation (refs ∩ entity-mention index ∪ participants' person entities ∪ the group entity); check-ins ask "what am I not listening to that I should be?"; deliberation that names a new entity proposes subscribing at next check-in | Under-subscription is the new silent-loss mode (replaces fail-open). Misses self-heal with latency bounded by check-in cadence — the right trade against a probe on every claim forever. |
| D9 | Subgoals are **in-place by default**; a child room only for a subgoal with its own stimulus surface (own subscriptions, own deadline, own multi-turn work); caps: ≤8 open children per parent, depth ≤3 | "Then confirm with Mike" is a next_action in the state block, not a room. Rooms spawn rooms only when decomposition is real, or the omnibus goal becomes sprawl. |
| D10 | Hierarchy = `parent_goal_id` (registry) + `report_to` (child room → parent room) | The `enqueue_revision` rollup + degrade-to-wake + wake-matrix stack collapses into one wake the *parent* reads in its own history. The wake matrix existed because children had no brain; now a child deadline nudges the child's room, which handles it or escalates upward. |
| D11 | **Lifecycle cascade at the close door**: `goal_close` on a parent requires children settled; parent cancel cascades to children with the reason recorded; the hygiene sweep prunes the subtree's routes | Today closing a parent orphans children silently — part of the drift problem. Orphan routes waking dead rooms is the "3 dead enabled routines" lesson again. |
| D12 | `goal_close` **requires evidence references** and validates them against the charter's success criteria where machine-checkable | The Meshy/phantom-ack lesson becomes doctrine at the door, not sentinels bolted onto result text. "Roster ⊆ persons-with-active-claims" is checkable in SQL; the WFH example below is the canonical case. |
| D13 | Check-ins are **wakeup series owned by the wakeups table** (rolls at fire — the routine-mirror invariant), created at room creation; no room exists without a next check-in | The creation-seed lesson from the 2026-09-16 overhaul, generalised. Cadence from charter/deadline (daily near a deadline, weekly otherwise). |
| D14 | `due` and all timestamps in the state block are **validated at write** (parse → reject → canonicalise, local ISO) | The bare-deadline wedge and the 2026-09-13 "23:28" misnarration are the same lesson: never trust LLM time formats at a boundary. |
| D15 | Approved **dream plans seed rooms** (`dream_plans.task_id` finally gets a writer) | Closes the loop the dream review identified: dream detects the commitment, the room works it. Charter from the plan's proposed action + assistance method. |

## Architecture

### 1. A room per goal

- Session key `agent:goal-<slug>:utility`; created by the goal tools when a goal
  is created (or migrated). `goals.conversation_id` points at it.
- Charter (injected per turn via the `utility_turn_spec` seam): objective,
  constraints, success criteria, report destination, and standing rules —
  close only with evidence; report stalls honestly; prune what you don't
  listen to. `model_alias` default `cheap`; the dashboard can bump one goal to
  main when it earns it.
- The room's turn: woken by steer (sensation), wakeup (check-in / due), or
  report (from a child). Full dispatch with workspace + goal + approval tools;
  reply stored to history; ends with a `room_state` tool call when anything
  changed. When the WhatsApp bridge is connected, rooms also get the **DM
  outreach tool** (`send_whatsapp_to_contact`, 2026-09-17 — the merch room's
  "can't contact Rupert" complaint): a room chasing an individual DMs that
  individual directly instead of waking the origin group to run a private
  errand (the 2026-09-14 WFH failure shape). Group-send tools are
  deliberately excluded — broadcasts keep the origin routing and the
  group-send approval gate. Outreach passes `parent_goal_id` so the child's
  settle rolls back into the room.
- State block (rendered into `strategy_json` for existing readers):
  `plan / known / open_questions / next_actions[{action, due}] /
  subscriptions` — the last being the room's own view of its routes, so the
  check-in conversation and the route table can't silently diverge.

### 2. Claim sensations (the broadcast layer)

- Emission: at the extraction post-loop (where `claim_router.refresh_mentions`
  runs today), after claims commit, emit one stimulus event per entity touched
  per turn: `source=memory`, `type=claim.write.<entity_id>`, body = claim
  summaries + kinds, `level` per D4 (action on supersession/correction, info
  otherwise). Durability rides the existing `memory.claims_created` event-log
  row + watermark; the heartbeat sweep replays gaps, as today.
- Routing: normal spine, normal valves. `MAX_EVENTS_PER_STEER` bounds backlog
  bursts; DEFER+coalesce batches them into one wake.
- Due nudges: the per-goal due pump fires `type=goal.due.<goal_id>` at the same
  door. The global pump that one bare deadline wedged no longer exists — one
  malformed goal defers itself.

### 3. Self-managed subscriptions

- Room tools: `subscribe(source, type_pattern, level)` /
  `unsubscribe(route_id)` / `list_subscriptions()` — validated
  (`_validate_spec` shapes), source-allowlisted (memory, calendar, goal,
  frigate at launch), capped at ~12 routes per room.
- Valve ceilings are constants, not room inputs (D7). The room may not create
  a route looser than the ceiling; the router enforces regardless.
- Hygiene: goal close/cancel prunes the subtree's routes; a reconciliation
  sweep (heartbeat, cheap) drops routes whose room is gone.

### 4. Nested rooms & hierarchy

- `goal_spawn(objective, charter_extras, due_hint, deadline)` — creates a child
  goal + room with `report_to` = parent room session, `parent_goal_id` set,
  routes seeded like any room. Enforces D9 caps.
- Child settle → final `send_report` wakes the parent; the parent folds it
  in-context on its next turn (which may itself be triggered by that report).
  No revision enqueue, no wake-matrix retargeting.
- Templates (`goal_templates.py`) mint root+children rooms unchanged in shape.

### 5. The registry

`goals` keeps: id, objective, kind, status, parent_goal_id, deadline, refs,
origin_conversation_id, room pointer (= conversation_id), external_ref.
Terminal transitions happen only through goal tools called by the room (or the
operator via dashboard/CLI, unrestricted as today). Split-brain rule: **the row
owns lifecycle, the room owns deliberation, one writer per kind** — status
columns are written by tools, state only by the room.

## What this replaces

| Today | Becomes |
|---|---|
| Reviser patch-writer + dict-op normalisation + JSON retry/degrade | Room's `goal_state` call, in-context |
| Claim-router structural matching + fails-open probe (1,268 calls/14d) | Subscription routes; probes → ~0 (residue serves legacy goals only) |
| `goal_revise` (1,133 calls / 19.0M tok per 14d) | Valve-bounded room turns, est. 20–40/day; fold + decide + report in one |
| Child rollup enqueue + degrade-wake + wake matrix | `report_to` on the child room |
| Global due pump (one wedge kills all) | Per-goal `goal.due` sensations |
| Review-loop deadline-window special-casing | Charters + check-in wakeup series |
| Deliberation inside human group chats | Rooms; `send_report` digests to origin |
| `goal_progress` main-model wakes (53k tok each) | Cheap room turn filters before anything expensive fires |

## Worked example — the WFH roster goal (the 2026-09-16 acid test)

Charter: "Obtain a work_schedule for every current AI Doom member. Close when
every member has an active work_schedule claim." Routes seeded on all members'
person entities + the group entity.

1. David posts his hours in the main group → silent-turn extraction writes
   `work_schedule(person-david-shedden)` → `claim.write.person-david-shedden`
   (info) → digest on the room's next turn → roster ticks David, evidence link
   to the source message.
2. Chris flips "Thursdays only" → "full-time" same day → supersession =
   **action** → immediate wake → state updated before close.
3. Andrea stays silent → her pending-ness is visible in the state block →
   check-in reports "missing: Andrea" to origin → Mike nudges, or the charter's
   patience rule reports the stall instead of wedging.
4. Close: `goal_close` validates roster ⊆ members-with-active-claims, each with
   evidence refs — machine-checkable, refused otherwise.
5. Post-close drift (Chris changes hours next month): the route died with the
   room; the correction updates **memory**, which the group-context push
   serves. The closed goal is a finished harvest, not a living document.

Also the cautionary tale: this exact incident produced David's schedule as
four near-identical claims in 4.5 hours — Bob's own narration re-extracted.
D6 is load-bearing, not hygiene.

## Rollout

Migration is nearly free right now: **one active goal** in the database.

- **Phase 1 — Rooms live.** Goal tools create rooms (charter, seed state,
  check-in wakeup series — D13); `goal_state` tool; dashboard goal pages read
  the room; reviser path intact underneath. Run the three WFH goals as the
  live test — they are the goals that just stalled.
- **Phase 2 — Sensations.** Claim-event emission (D4/D5 tiering), `goal.due`,
  subscription tools + hygiene sweep; claim-router matching/probe switches to
  legacy-goals-only.
- **Phase 3 — Hierarchy & dream.** `goal_spawn`, lifecycle cascade, dream-plan
  seeding (D15).
- Kill switch: `BOB_GOAL_ROOMS=off` — new goals take the legacy path, rooms go
  inert, reviser serves everyone again. Legacy goals finish on the reviser and
  are not migrated.

## Testing & verification

- Self-echo (D6): a claim whose origin is the room's own conversation does not
  wake it; room turns leave no extraction claims (synthetic marker).
- Tiering (D4): supersession → action-level; add → info, delivered as digest on
  the next turn, never a standalone wake.
- Coalescing (D5): N claims over one entity in one turn = one event; a burst
  over cooldown = one wake carrying the backlog, MAX_EVENTS_PER_STEER honoured.
- Close door (D12): WFH-roster case as fixture — close refused while a member
  lacks an active claim; accepted with evidence refs; parent close refused
  with open children; cancel cascades and prunes routes.
- Check-ins (D13): a room without a pending check-in wakeup is a test failure;
  cadence rolls at fire (wakeups own the schedule).
- Write-time validation (D14): bare deadline, prose timestamps rejected and
  canonicalised at `goal_state`, not downstream.
- Subscription management (D7/D8): valve ceilings enforced on create/edit;
  source allowlist; route cap; seeded set = entities linked at creation.
- Fallback: `BOB_GOAL_ROOMS=off` from a running rooms state → legacy path
  serves goals, no orphan routes fire.
- Smoke: the three WFH goals, one week — watch journal fold lines vs the
  81/day reviser baseline; every wake traceable to a route or wakeup.

## Open questions

- **Roster semantics for "everyone" goals**: membership at charter time, with
  the group-entity subscription delivering joins/leaves — does a join
  mid-goal extend the charter's "everyone" or need a charter amendment?
- **Which sources at launch**: memory + goal certain; calendar, frigate
  desirable (a room subscribing to `activity.person.david` to catch him
  on-site is the poster child) — sequence them by need.
- **Check-in cadence policy**: fixed per charter vs state-block-derived
  (`next_action.due − 12h`, the due-action lesson) — start fixed, derive later.
- **Model bumps**: who may move a room from cheap to main, and whether a
  charter may *request* it (operator flip only, v1).
- **Room history growth**: per-goal compaction policy for long-lived rooms
  (charters re-inject per turn; history folds like any conversation — measure
  before building).
