# Goal execution loop — self-scheduling turns, strategy trees, one goal shape

Status: **proposed** (2026-09-21). Origin: Mike's ask — technical goals
("research", "build things") that need multiple strategies considered and
tested, multiple subagents, and a review/restrategise pass after *every*
turn don't fit clock-driven check-ins; negotiation goals (events) need
patience and human-channel coordination instead. Live evidence: the
2026-09-20 dead-effect pile (7 stale-version rejections in 3 days —
concurrent writers colliding), the WFH-roster stall (2026-09-16), and the
eCourts crawler goal's strategy churn (3 superseded state writes in an
hour). Builds directly on goal-rooms-plan.md and task-registry-plan.md;
this plan is their execution model.

## The problem, concretely

The room gives a goal a working surface and attention; the check-in gives
it a pulse. But the pulse is **clock-driven and fixed** (`+1440m`/
`+10080m` recurrence): it fires when nothing changed and waits when
something did. For exploratory work every dimension is wrong:

| mismatch | today | needed |
|---|---|---|
| timing | fixed interval | event-driven (experiment finished → review now) |
| state | linear worksheet (plan/next_actions) | strategy tree: hypotheses, evidence, pruned branches |
| review | improvised by the woken turn | a structured turn type every time evidence lands |
| concurrency | any conversation may CAS-write state (stale-version collisions) | append-only evidence from anywhere; one writer |
| human coordination | separate outreach machinery | same loop, tasks pointed at channel conversations |

And three wake species (check-in, action_due, goal_scan) encode three
schedules where one decision — "when should this goal next speak?" —
would do.

## Component definitions

A goal is **a conversation with a memory, a wallet of tasks, and a rule
for when it speaks next.**

| Primitive | Question it owns | Shape |
|---|---|---|
| **Room** (exists) | where turns happen | utility conversation + charter |
| **State block** | what we know, what we're trying, what's next | small structured worksheet incl. the strategies list |
| **Task** (exists) | who owes what, did it arrive | promise row: waiter/completer/result/due |
| **Continuation** | when does this goal next speak | one pending `goal_continue` wakeup per goal |

`kind` (research | build | negotiate | …) selects **only** the charter
template and tool emphasis — never a different engine. Research tasks
point at subagents; build tasks at scripts/verification; negotiation
tasks at channel conversations. The loop machinery is identical.

## Settled design decisions

| # | Decision | Why |
|---|---|---|
| D1 | **Continuation is a hint over a deterministic spine.** A turn MAY declare `continue_now` / `wait(minutes\|until)`; the system guarantees a next wake regardless: no declaration + pending tasks → event-driven; no declaration + no tasks → dead-man heartbeat. | The send-skip lesson (~20% of turns drop required final calls): scheduling must survive the model forgetting. The declaration optimises the default; it never replaces it. |
| D2 | **One pending continuation slot per goal** (keyed upsert on `goal_id`, kind `goal_continue`); **event beats timer** — any event wake that fires (task settle/completion, deadline) consumes the slot; a new declaration replaces the old one. | Prevents double wakes (timer fires 28 min after the event already ran the turn) and keeps "when does this goal next run?" one inspectable row. |
| D3 | **Dead-man heartbeat**: a recurring long-interval wakeup created with the goal; `wait()` may extend the next occurrence but never past one interval. | A goal with a forgotten declaration AND dead tasks still surfaces. Replaces the fixed check-in series as the liveness floor. |
| D4 | **State block reshaped**: `objective` (immutable at creation), `known` (append-only evidence lines: who/when/what), `approach` (one paragraph + next steps), `strategies: [{hypothesis, status: candidate\|testing\|won\|pruned, verdict}]`, `budget` (rounds/tokens remaining). Evidence appends from ANY conversation (no CAS); only the room's own turns rewrite the tree. | Multiple-strategies memory without a new subsystem; kills the stale-version collision class — the common case stops being a race (2026-09-20: 7 dead rejections). |
| D5 | **Tasks are the universal branch.** Experiments, build/verify steps, and negotiation confirmations are all task-registry rows. Negotiation: task with `expected_completer` = the channel conversation; the channel turn settles it on the counterpart's reply (the tasks-block injection already makes that turn aware); `task_due` backstop IS the follow-up ladder. | Zero new engine — this is the task-registry's own outreach composition. One primitive covers "spawn experiment" and "get Thomas's confirmation". |
| D6 | **Review is a turn type, not an entity.** Evidence-driven wakes carry the review frame ("evaluate this evidence, update strategies, spawn/close/prune/block"). A deterministic micro-judge in the settle path (delta: tasks spawned? settled? state version? sends?) selects the stall frame on zero delta: "you must prune, close, or declare blocked". | The reviser autopsy: it failed on inputs (transcripts, not evidence), non-actuation, and being a second writer/scheduler. Frames keep the discipline, drop the pathology. A frame can be dispatched to a cheaper model category later without entity-hood. |
| D7 | **Budgets and spin rails**: max 3 consecutive `continue_now` (then forced wait), per-goal round/token budget checked by the frames, and the hardened no-op rule (a turn that changes nothing must prune, close, or declare blocked). | Exploratory loops + GLM tool odysseys = unbounded furnace otherwise (2026-09-01: 60+-iteration turns). |
| D8 | **goal-craft skill** (lazy, one index line): example-driven writing guide with bad→good pairs per kind, failure patterns from real incidents, the "done looks like ___" evidence contract. Bundled per-kind example files under `skills/goal-craft/`; routed by the `trigger:` frontmatter line + a one-line pointer in the `create_goal` tool description. Ships in workspace AND the repo skills bundle. | Judgment quality (scope, termination evidence, kind choice) can't be machinery — but it must arrive at the creation moment, cost ~150 chars otherwise, and be eval-tested (every worked example becomes an eval case) or it decays on the next model switch. |
| D9 | **Kill switch** `BOB_GOAL_LOOP=off`: rooms fall back to today's fixed check-in cadence; continuation/frames go inert. | House convention; the fallback already exists. |
| D10 | **The dashboard is the follow-along surface**: a goal list (next-run, branch count, budget burn, stall flags) + a drill-down page (state block, strategies tree, task history, turn/wake timeline) answering "what is this goal doing and why" in one screen. Read-only, derived from existing tables (goals/tasks/wakeups/messages); the only UI writes are the existing cancel plus nudge (wake now) and pause (cancel the continuation slot). | An autonomous loop Mike can't watch is a loop he can't trust — observability is a release gate for Phases 2–3, not decoration. The single continuation slot (D2) is what makes "next speaks in 27m" a real, inspectable answer. |
| D11 | **The goal's voice reaches the origin conversation** (Mike's decision, 2026-09-21): completions, blocked/stall declarations, and budget requests all report to the origin — via the existing `send_report`/`report_to` wake delivery, inheriting its group gates where the origin is a group. The dashboard is pull-only: no proactive goal notifications to Mike's DM, no dashboard push. | One channel per goal's voice — the conversation that asked for the goal hears its outcomes. Keeps Mike's DMs signal-only while the dashboard serves following-along. |
| D12 | **Goals are flat — no child goals** (Mike's decision, 2026-09-21). One goal = one objective = one room. A sub-outcome that needs its own deliberative loop is a SIBLING goal created with origin = the creating room, and the creator holds a TASK on the new room (`expected_completer` = its session key) — the task registry is the DAG edge. `parent_goal_id` goes dormant (kept for history, like persona_records); roll-up, depth caps, and root-wake targeting are retired. Aggregate views group by origin chain in the dashboard. | Once tasks are the universal branch, nesting is a second mechanism doing the same job — and the hierarchy machinery (roll-up, `max_goal_depth`, root-targeting rules) has been the fiddliest part of the goal stack. Scope control shifts to creation time, where the goal-craft skill and evals can police it. |
| D13 | **Budgets are rounds; exhaustion is a forced terminal round, never silent death** (Mike's decision, 2026-09-21). Unit = one room turn (wake → settle), spent in the settle path. 70% → frames carry a dwindling warning; 100% → the next round is forced to the terminal frame (complete with findings / declare blocked / request renewal); a turn in flight always finishes. Refill is an origin-only `goal_refill` bump (dashboard operator override); the room can ask but never refill itself. Subagent/script costs are not metered — guarded by the concurrent-task cap instead; token attribution can be added later if real spend diverges from rounds. | The failure mode being guarded is loop churn, which rounds measure directly and tokens don't; and terminal-round semantics mean exhaustion always produces an outcome + a report to the origin (D11), never a goal that quietly went dark. |
| D14 | **Build goals: everything up to green tests on a branch; merge/deploy is a human gate** (Mike's decision, 2026-09-21). A build goal may write code, run the suite, iterate on a branch within budget — its "done" proof includes tests green. Merging to master, `systemctl restart`, and any production deploy are NOT in-goal: the goal requests them via the origin conversation (same channel as budget renewal), and the origin turn (or Mike) executes the promotion. | The house rule — commit/deploy only when asked — predates goals and survives them; a goal whose completion criteria silently include a prod deploy would make the autonomy grant unbounded. Uniform ask-in-origin pattern with D13 keeps one approval surface. |

## What this replaces (retired in Phase 6, after burn-in)

- Fixed `goal_checkin` recurrence → dead-man heartbeat + continuation.
- `action_due` scanning (`schedule_due_action_wakes`) → `wait_until`
  declarations + task dues (calendar behaviour falls out of the same
  primitive).
- `goal_scan` → a recurring task or standing `wait` pattern.
- The `next_actions`/`open_questions` worksheet fields → `approach` +
  tasks + the strategies list.
- The parent/child hierarchy: roll-up on settle, `max_children_per_parent`,
  `max_goal_depth`, root-targeting of child deadline wakes (D12). Existing
  parented goals render as-is (column dormant); no new nesting.

Not replaced: sensations (unchanged broadcast layer), task registry,
deadline wakes (kept for goals that genuinely have external clocks),
`task_due` backstops, `reconcile_orphans`.

## Architecture

### 1. Continuation contract

End-of-turn states (charter text + structural enforcement):

```
continue_now(reason)      → immediate follow-on turn (max 3 consecutive)
wait(minutes | until_iso) → validated timer into the goal's single slot
(rely on pending work)    → implicit; event wakes are the pendulum
complete / fail           → existing tools; slot cancelled, series ends
```

Mechanics: new wakeup kind `goal_continue`; upsert keyed on
(goal_id, kind) — cancel-then-schedule in one transaction; any event wake
that fires for the goal cancels the pending slot first. The settle path
(after each room turn settles — `on_turn_settled` seam) computes the
deterministic default when no declaration was made and logs a
`continuation-skip` counter (the send-skip metric's sibling). Interval
args are typed and validated at write time — no prose timestamps (the
40-minute-show lesson).

### 2. State block and tools

```
strategy_open(hypothesis, first_experiment)      → strategies[] += candidate
strategy_result(strategy_id, evidence, verdict)  → append evidence, set verdict
strategy_prune(strategy_id, reason)              → pruned with reason recorded
goal_continue(...) / goal_wait(...) / goal_done  (the contract, as tools)
```

Evidence appends (`known` lines, `strategy_result`) are unrestricted —
any conversation settling a task for the goal may append. Tree rewrites
(`approach`, prune, budget spend) are room-only; the version guard
remains as the backstop for true races.

### 3. Frames

Two wake-brief templates, selected deterministically:

- **Review frame** — used when the wake cause is evidence (task settled
  with result, inbound-settled confirmation): "evaluate this evidence
  against the objective; update the strategies tree; spawn the next
  experiment or the next ladder rung; declare done only with evidence."
- **Stall frame** — selected by the zero-delta micro-judge: "this turn
  changed nothing. Prune a strategy, close the goal with evidence, or
  declare blocked to the origin. A no-op turn is a failure, not rest."
- **Terminal frame** — forced at budget exhaustion (D13): complete with
  findings, declare blocked, or request renewal in the origin.

Charter templates per kind carry the execution playbook (research:
branch/test/prune, prefer evidence; build: nothing is done until the
artifact passes its declared proof — tests green on a branch, visual
verification where the artefact is visual; merge/deploy is requested,
never performed, per D14; negotiate: confirmations are tasks, follow the
ladder, never post status checks to groups).

### 4. Budget mechanics (D13)

One round = one room turn, spent in the settle path (same hook as the
delta micro-judge). Parallel exploration is taxed naturally: experiments
completing → event wakes → review rounds; event-beats-timer coalescing
is the discount. Thresholds: **70%** — frames carry a "budget dwindling:
consolidate, decide, or request renewal" line; **100%** — the next round
is forced to the **terminal frame** ("budget exhausted — complete with
findings so far, declare blocked, or request renewal"), after which the
continuation machinery refuses to schedule. Outcomes: complete (evidence
recorded, origin informed), blocked (parked; wakes again only when the
origin answers), or a `send_report` renewal request to the origin.
Refill: `goal_refill`, callable only from the origin conversation
(dashboard operator override); the room can request but never spend
refills. Visible as the state-block budget field, list/drill-down bars
(D10), and per-round spend in the timeline. Not metered: subagent and
script costs — bounded by the concurrent-task cap; token attribution is
a later addition if real spend diverges from rounds.

### 5. Observability — goal list and drill-down (the follow-along surface)

**List** (`/goals`, rebuilt): one row per goal — status, kind, objective,
link to the room; **next run** read from the continuation slot ("speaks in
27m on task settle" / "idle — dead-man Friday" / "waiting on 2 branches");
pending-branch count; budget burn; time since last evidence delta;
stall/continuation-skip flags. Filter by status/kind; sort by next-run.
Today's routing-decision panel (probe/revise verdicts) is removed with
the machinery it described.

**Drill-down** (`/goals/$goalId`, new):

- **Header**: objective (immutable), kind, status, budget bar, origin
  conversation link, created/deadline.
- **State panel**: `approach`, the `known` evidence feed, and the
  strategies tree with status chips (candidate / testing / won / pruned)
  and verdicts — pruned branches stay visible with their reasons, so
  "why did we abandon approach B" is always answerable.
- **Branches**: every task tied to the goal (`source_goal_id` or
  waiter = room) — title, completer (subagent vs channel conversation),
  due, status, result excerpt, link to the settling conversation. This is
  where negotiation ladders and experiment fleets both read.
- **Turn & wake timeline**: wakeups fired (kind + cause), each room turn
  with its continuation decision (continue_now chain, declared wait,
  spine-covered skip, stall-frame turn) and its delta classification —
  merged with task settles and state rewrites into one evidence
  timeline. This is the "everything I need to follow this goal" view.

**API** (`dashboard_api/goals.py`): `GET /api/goals/{id}` returning the
parsed state block, strategies, budget, the pending continuation row,
the goal's tasks, and the recent wake/turn history (delta-classified).
List endpoint gains the same summary fields. No new write endpoints
beyond nudge/pause (both reuse existing wake/cancel primitives).

### 6. Negotiation composition (worked)

"Negotiate the Saturday event with Thomas":
1. Goal kind `negotiate` → room + charter; objective names the outcome
   and its proof ("both a confirmed date from Thomas and a booked venue
   reference").
2. Room opens tasks: `obtain Thomas's date confirmation`,
   `expected_completer = agent:main:whatsapp:dm:614…` — the DM
   conversation gets the tasks-block injection; when Thomas replies, that
   turn settles the task with the quoted reply; the settle effect wakes
   the room with the result (review frame).
3. No reply by due → `task_due` backstop wakes the room → charter's
   ladder: re-DM (new task, longer due), then email, then call.
4. All confirmations settled → room completes with evidence; origin
   conversation gets the result.

Research ("make headless WebGL work") differs only in step 2: tasks
point at `script`/`claude` subagents; each completion wake is a review
turn; strategies tree records pruned approaches with verdicts.

## Rollout

| Phase | Content | Gate |
|---|---|---|
| 1 | Continuation contract: `goal_continue` kind, single-slot upsert, event-beats-timer, settle-path default + skip logging, dead-man heartbeat; `BOB_GOAL_LOOP` switch (default off) | unit + E2E tests; flip on for ONE pilot room (careful-rollouts convention: seed, then verify on a real goal before wide enable) |
| 2 | State reshape: strategies list + evidence appends + budget field; strategy/continuation tools; charter text updates | pilot goal's state renders correctly across 10+ turns |
| 3 | Frames: review frame on evidence wakes, stall frame + zero-delta micro-judge; charter templates per kind (research/build/negotiate kinds added to `room_kinds`) | each kind runs one real goal end-to-end (merch-style live test) |
| 4 | Observability: `GET /api/goals/{id}` + list summary fields; rebuilt `/goals` list (next-run, branches, budget, flags); new drill-down page (state panel, strategies tree, branches, timeline); nudge/pause actions | Mike can follow the pilot goal end-to-end from the dashboard without opening a DB — verified visually (screenshot walkthrough of list → drill-down → timeline against the live pilot) |
| 5 | goal-craft skill (workspace + repo bundle), `create_goal` pointer, eval cases from the worked examples | eval suite green on current model; skill index delta = 1 line |
| 6 | Retire `action_due` scanning, `goal_scan`, fixed check-in recurrence; default `BOB_GOAL_LOOP=on` | metrics gate below, one clean week |

## Testing & verification

- Unit: slot upsert CAS + event-beats-timer; interval validation
  (reject prose); delta scoring; strategies schema; evidence-append from
  a non-room conversation (allowed) vs tree rewrite from one (rejected).
- E2E: pilot room across continue_now → wait → task-settle wake →
  stall-frame turn; negotiation composition with a fake DM completer.
- Metrics (log lines, reviewed before Phase 5): continuation-skip rate
  (if constant, the spine is carrying the loop — acceptable, but the
  declaration becomes deletable noise); no-op/stall-frame rate (should
  decline as charters bed in); stale-version rejections (should → ~0).
- Eval: every goal-craft worked example becomes an eval case
  ("given this vague ask, write the goal") so the quality bar survives
  model switches.
- UI (Phase 4): component tests for the list/drill-down against the new
  API shape; visual verification of every screen against the live pilot
  goal before sign-off — list row, drill-down tabs, strategies tree
  rendering, timeline merge (Mike's standing rule: nothing visual is
  "done" from code alone).

## Open questions

1. Frames on the cheap model tier for routine reviews, escalate to main
   on stall? (Deferred until skip/no-op rates are known.)
2. Parallel experiment cap per goal — 2 or 3 concurrent tasks?
3. Do `research` goals keep deadline wakeups at all, or is
   budget-exhausted the only clock? (Resolved for notifications by D11:
   budget requests go to the origin.)
4. Sensation "wake tier": do urgent entity mentions (negotiation
   counterpart named the event in a group) wake the room immediately
   instead of waiting for the next check-in digest?
5. Should the drill-down timeline also render the room's outbound
   messages (the goal's voice), or only system-visible events (wakes,
   tasks, state)? Messages are one query away; noise vs follow-along
   value decides it during Phase 4 on the pilot.
