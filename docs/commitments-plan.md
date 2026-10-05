# Commitments — one model for what Bob owes, is doing, and will do

Proposal 2026-10-05. Synthesises the tasks/goals/plans/backburner discussion.
Phase 4 (dream suggestions) is specified in
`docs/dream-goal-suggestions-plan.md`.

## Diagnosis

Bob tracks work in eight places, each grown to fix one incident:

| Concept | Where | What it actually is |
|---|---|---|
| goal | `goals` (15 free-text kinds) | an outcome to pursue — OR execution bookkeeping |
| task | `tasks` | a promise between conversations, settled once |
| dream plan | `dream_plans` | a suggested goal with its own status machine |
| subagent | `subagents` | an executor run — OR a detached turn — OR a script |
| detached flight | `subagents` + `goals` | an execution of a conversation turn |
| bg job | `bg_jobs` | an executor run |
| routine | `routines` + `wakeups` | a standing, recurring obligation |
| agenda | session policy | standing instructions |

Live data, 2026-10-05:

- ~1,170 of ~1,280 goals are `kind=subagent` bookkeeping (725 detached
  flights, 421 retired scripts, a handful of real subagents). Real outcomes
  (outreach, events, builds, sales targets) are ~90 rows. goals table,
  goals_block and dashboard are >90% execution noise.
- `subagents` is mostly not subagents: 762 detached turns + 421 scripts vs
  ~50 genuine agent runs.
- Goal `kind` is free text with 15 values; `event` vs `event_plan` silently
  decided room/loop/playbook (2026-09-25 incident).
- The model faces ~20 work tools across overlapping concepts.

Recurring failures live in the seams between concepts, not inside any one:
promised work never registered (G3), phantom narrated work, the
`plan_complete` retry loop, room→group delegation that can't resolve, the
2026-10-03 holding ack promising work that never ran.

Root cause: intent, execution and time are conflated.

## The model: three orthogonal layers

```
COMMITMENTS  — what is owed / wanted        (intent; a tree)
     │ executed by
RUNS         — who is doing it right now    (execution)
     │ scheduled by
WAKEUPS      — when something must happen   (time; already unified)
```

### 1. Commitments — evolve `goals`

Keep the table: CAS `version`, `goal_transitions` audit, rooms,
`loop_state_json`, creator pinning all survive.

- `profile` — closed enum that decides behaviour:
  - `promise` — leaf owed by a completer (today's task: CAS-once settle via
    the `task_settle` effect, due backstop, completer-side visibility)
  - `outcome` — multi-step objective (today's goal: strategy, reviser, may
    get room + loop)
  - `standing` — recurring obligation (today's routine)
- `label` — free text for humans/model; never routed on.
- `status` — `suggested → active → waiting → completed | failed | cancelled`.
  `suggested` is dormant (no wakes, no room, no loop, no outreach);
  `waiting` = blocked on children or an external party.
- `owner_conversation` (works it), `for_conversation` (owed to; woken on
  settle), `completer` (promises), `parent_id`, `due_at`.
- Settles roll up: a child settling wakes the parent's owner (generalises
  what the goal loop already does for tasks).

Collapse map:

| Today | Becomes |
|---|---|
| task | `promise` |
| real goal | `outcome`, `label` = old kind |
| dream plan (+ its offer-task) | one commitment in `suggested` |
| routine | `standing` (recurrence on its wakeup series) |
| goal kind=subagent | not a commitment — a run |

### 2. Runs — one read model over executors

Thin `runs` table: `id, kind (flight|agent|job|call|turn), commitment_id,
session_key, dispatch_id, status, started_at, ended_at, external_ref`.
Every executor writes it on start/terminal. Physical plumbing tables stay
(`bg_jobs` for systemd, `phone_calls` for Twilio). Dashboard, honesty ledger
(tool-call counts) and attribution read runs.

Detached flights stop creating goal and subagent rows. The detach-v2
transcript placeholder + attribution token already provide the visibility the
goal row was added for.

### 3. Wakeups — keep, tighten

Every wake carries `commitment_id` instead of kind-specific payload keys
(`task_id`, `routine_id`, `goal_id`): the four `cancel_for_*` helpers become
one `cancel_for_commitment`.

## Capability wins

1. **One "Work" prompt block** replacing goals_block + tasks_block + plan
   announcements: *You owe* / *Owed to you* / *Suggested*.
2. **Six tools instead of ~20:** `add_commitment`, `close_commitment`
   (outcome = completed | failed | cancelled), `list_commitments`,
   `delegate_commitment`, `schedule_commitment`, `accept_suggestion`.
   Idempotent closes everywhere (plan_complete loop lesson); refusals carry
   the reroute inside `delegate_commitment` (room→group lesson). Smaller
   surface targets GLM-flash tool-selection errors.

   Naming rule: verb + `commitment` noun, never a bare verb. Review
   2026-10-05: bare `commit` collides with `git commit` (Bob codes in the
   workspace via bash/subagents — a small model resolves ambiguity to the
   dominant sense, and a promise "registered" via `git commit` fails
   silently, the exact G3 class), and bare `settle` collides with payment/
   trade settlement (merch PayID matching, cryptobro). The shared noun also
   groups the family in the tool list.
3. **Enforceable promises.** One ledger makes the end-of-turn check
   mechanical: final text promising future work with no `add_commitment`
   this turn gets one self-wrap-style nudge round. G3 stops being an
   ignorable prompt rule.
4. **Decomposition with dependencies.** Outcomes spawn promise children with
   completers (people, conversations, jobs); parent sits in `waiting`;
   roll-up wakes the owner. Today this is hand-wired per feature.
5. **Dashboard Work page:** commitment tree, each node's runs, next wake,
   links into turn-timeline traces.
6. **Dream suggestions for free:** the dream pass writes commitments in
   `suggested`; the suggestion itself is the pending offer (no separate
   offer-task); a reply calls `accept_suggestion`.

## Model capability — designing for GLM-5.3-flash

Flash is reliable at single local steps (add one commitment for an explicit
ask, close one child when its wake says what to do, start one job) and
unreliable at open-ended planning, long repetitive call sequences, and
unprompted judgment calls. Known record: ~20% final-send skip, narrated
tool calls, wording-resistant on the bg_delivery battery.

Principle: **the system carries the long horizon; the model makes only
local decisions** (same stance as the goal loop — declarations are hints
over a deterministic spine).

1. **System-guaranteed spine.** Roll-up wakes, job-exit wakes, `waiting`
   status and due backstops need no model. Every wake names the commitment
   it is for and the expected next action ("Sam's model finished — close or
   retry"), so the model never has to remember which run served which
   child.
2. **Batch creation.** `add_commitment` accepts a list of children in one
   call (N calls → 1), removing the skipped/duplicated-item failure of long
   repetitive sequences.
3. **Spend gate as a system rule.** A `delegate_commitment` that starts paid
   or irreversible runs above a threshold auto-inserts an approval child
   (completer = the owner) before the run starts — the checkpoint exists
   even if the model never thinks of it. Commitments may also carry a
   budget and a concurrency cap for child runs (e.g. Runware 3D
   concurrency, credit spend).
4. **Planning playbook.** An outcome room's first turn gets a "decompose
   this" frame plus the goal-craft skill (uplifted, below) instead of
   improvising.
5. **Stronger model for big rooms.** Outcome rooms may run on a stronger
   model (sol/astra; per-conversation models already work) while chat stays
   on flash — a few planning turns, not every message. Default decided by
   the eval below.

**Eval (Phase 3 battery):** fan-out case — "make 3D figurines for these N
people based on what you know about them". Pass = one parent outcome with N
children (batch call), an approval child exists before any paid run starts,
each run linked to the right child, no child closed without a result.
Run on flash AND sol: if flash fails planning but passes the per-step
cases, outcome rooms default to the stronger model.

## goal-craft skill uplift

`skills/goal-craft/` (skill.md + research/build/negotiate examples) is
written against the current model and would contradict the new one:

- After Phase 1 its "What kind is it?" section teaches routing by
  `research | build | sales_target | negotiate | event_plan | task` —
  routing that no longer exists once behaviour moves to `profile`.
- After Phase 3 it names `create_goal` (×3, including the **trigger line**
  that decides when Bob loads it), `goal_continue_now`, `goal_wait`.

Uplift, not replace — the three rules (name the artefact + proof; one
objective; branches over plans) and the bad→good example pairs stay.
Changes:

1. Profile choice (`promise | outcome | standing`) replaces kind choice;
   `label` is free description.
2. Decomposition shapes: child commitments vs sibling outcomes —
   reconcile rule 2 ("two goals → siblings") with parent/child trees.
3. Fan-out pattern: one parent, N per-item children in one batch
   `add_commitment`, each with a named completer.
4. Checkpoints: approval child (owner as completer) before paid or
   irreversible steps — taught as deliberate practice; the system spend
   gate is the backstop.
5. New `examples/fanout.md` — the figurines request as its bad→good pair.
6. Trigger + "During the run" rewritten for the new tools
   (`schedule_commitment` replaces continue/wait).

## Phasing — each phase ships alone

| Phase | Change | Gate |
|---|---|---|
| 0 — stop the bleeding ✅ 2026-10-05 | Flights stop writing goal+subagent rows (bg jobs already didn't); `runs` table (016) + backfill (764 flights, 421 scripts); goals_block drops `kind=subagent` goals and gains a *Running in the background* section (flights + live subagents); check/kill_subagent resolve flight ids via runs; dashboard goals page intent-only, subagents page keeps flights; legacy flight-goal recovery kept for one release | backburner suite + full suite |
| 1 — profiles ✅ 2026-10-05 | `profile` outcome\|promise (017); room/loop route off `profile`; kind is a free label (aliases advisory only); `BOB_GOAL_ROOM_KINDS` removed | full suite |
| 2 — promises join ✅ 2026-10-05 | Tasks copied in as `kind='promise'` rows (018, 171 rows, ids kept); `TaskRepository` reads/writes goals; `tasks` frozen. *Skipped:* wakeups `commitment_id` — `task_due` keeps `payload.task_id` (same id, nothing gained) | full suite |
| 3 — Work block + six tools ✅ 2026-10-05 | `BOB_GOAL_TOOLS_V2` (default on): six tools, Work block, batch children, decompose-first room opening, goal-craft uplift + `examples/fanout.md`; evals `commit_guard_coding_promise_recorded` (flash 3/3) + `fanout_room_decomposes_with_approval` (flash 1/2, sol 1/1). Battery 2026-10-06 on the 39 cases free of network errors: v1 29 = v2 29; goal cases v2 ≥ v1, fan-out v1 1/4 vs v2 4/4 (after fixing an eval fixture where shadow tools masked the v2 tools). LIVE 2026-10-06. *Deferred:* system-enforced spend gate (needs a definition of "paid") | battery v2 ≥ v1 |
| 4 — suggestions ✅ 2026-10-05 | Announce writes a `suggestion` row instead of an offer-task; `accept_suggestion`/`close_goal` act on it; plan tools hidden in v2. `dream_plans` stays as the dream's private notebook (see dream-goal-suggestions-plan.md) | full suite |
| 5 — standing ✗ dropped | Routines stay separate (decision 3) | — |
| 6 — Work page ✅ 2026-10-05 | `/dashboard/work`: goal tree with promises, principal, next wake, live runs → trace; standalone promises; suggestions; background runs | visual verify |

Phases 0–1 are worth doing even if the rest never happens; Phase 0 alone
removes ~90% of goals-table noise.

## Risks

- Goal loop and rooms are the most intricate code here — no phase rewrites
  them; the table evolves underneath.
- Tool-surface swap is the riskiest behaviour change — old tools stay wired
  until the battery scores new ≥ old.
- Live state migrates (5 pending tasks, 1 active sales-target goal, open
  wakeups) — dry-run, idempotent scripts at a quiet hour.
- Agenda (`update_agenda`) is standing instructions, not work — deliberately
  out of scope; belongs with conversation policy.
- Information scope: rooms inherit the creator's principal, so what Bob may
  use about people depends on who asked (owner = full memory; a less
  trusted member = less). Correct, but it bounds output quality for
  person-based work — the Work page should show the principal a room runs
  under.

## Decisions (Mike 2026-10-05)

1. **Name: keep `goals`.** "Commitment" stays the concept word in this doc;
   the table, code and tools say goal. Tools: `add_goal`, `close_goal`,
   `list_goals`, `delegate_goal`, `schedule_goal`, `accept_suggestion`
   (still verb + noun — never bare `commit`/`settle`).
2. **Unregistered promises: eval-only.** No nudge round; the battery
   measures G3 and the git-commit guard case.
3. **Routines stay separate.** Phase 5 is dropped.
4. **Goal rooms run on flash** (2026-10-06). Already the case: rooms are
   utility conversations with `model_alias='cheap'` → `z-ai/glm-5.3-flash`.

Mike: "continue and work through all phases."

## Cleanup (2026-10-06, Mike: "do it now")

v1 surfaces deleted: `BOB_GOAL_TOOLS_V2` and `BOB_TASKS` kill switches,
`work_names.tn()` (prompt text names the v2 tools literally), v1
goal/task tool sets (kept only as internal handler sets
`goal_tool_handlers` / `promise_tool_handlers`), `tasks_block`, the dream
plans prompt + plan tools (`dream/tools.py`, `dream/injection.py`), the
offer-task announce branch, legacy flight-goal recovery, and the subagent
goal fallbacks. `tasks` table dropped (019, archived). `update_goal` /
`update_goal_state` take `expected_version` optionally (flash omitted it).

The old `/goals` list merged into the Work page 2026-10-06 (loop status, cancel, settled history); `/goals` redirects to `/work`, goal detail stays at `/goals/<id>`.
