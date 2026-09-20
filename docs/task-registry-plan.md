# Task Registry — durable promises between conversations

Status: **proposed** (2026-09-19). Origin: Mike's ask after the OOM-orphaned
Meshy job (2026-09-19 09:09 AWST: a killed poller left an active wrapper
goal, a submitted-but-unwatched Meshy job, and no path that would ever report
completion) and the observation that Bob runs five half-versions of the same
callback pattern. Final phase retires most legacy goal machinery per the
goal-rooms end-state. Prerequisite flagged: the eval harness tool-loop must
be fixed before Phase 2 flips outreach (it currently makes zero tool calls).

## Component definitions (the boundary contract)

Four primitives, one job each. Everything else composes them.

| Primitive | Question it owns | Shape | Lifetime |
|---|---|---|---|
| **Goal** (a room) | "What are we trying to achieve, and what's next?" | charter + history + state block + check-ins; formulates work | long-horizon, deliberative |
| **Task** (this plan) | "Who owes me an answer, and did it arrive?" | a promise row: waiter, completer, result, one wake | short-lived, point-to-point |
| **Sensation** | "What changed in the world that I care about?" | event → routes → subscribers, valved | instantaneous, broadcast |
| **Wakeup** | "When should I look again?" | time-based wake series | scheduled |

**Transport, not concepts:** *steer/wake* deliver instructions and results
between conversations; *effects* make every state change durable and
idempotent. A task uses steer (to delegate), wake (to return), effects (to
survive crashes), wakeups (as its liveness floor), sensations never (they
are broadcast; tasks are point-to-point).

**Explicitly NOT an execution engine.** A task holds a promise, never work
state — no scheduling, priorities, or decomposition (that's goals' job). The
repo deleted one task engine already (legacy tasks/projects, commit
`8c3d016`); this is the narrow concept that survived the lessons: the
callback.

## The problem, concretely

Today's five implementations of waiter/completer/waker, with different
durability and failure semantics:

| Mechanism | Register | Complete | Wake waiter | Restart-safe |
|---|---|---|---|---|
| Outreach goals | `send_whatsapp_to_contact` mints goal in target DM | `finish_outreach` | settle → origin wake | ✅ |
| Subagent wrapper goals | parent spawns subagent | subagent result | origin wake | ❌ (2026-09-19 incident) |
| bg tasks + relays | origin detaches | task end | relay to origin | partial; one destination |
| `report_to` | charter names target | utility turn | wake target | ✅ |
| Goal deadlines | time liveness | — | wake | ✅ |

And the two process-side gaps: `bg_start` scripts can only write log files
(no callback path at all), and detached turns die with the service. The
gnome run hit all of it: job submitted, poller killed by OOM, wrapper goal
active-but-orphaned with no deadline, failure notify raced the DB close, no
message would ever have arrived.

## Settled design decisions

| # | Decision | Why |
|---|---|---|
| D1 | New `tasks` table — not a goal variant, not a new "engine" | A promise is a different concept from intent; overloading goals is what created the five versions. One table, one repo, one lifecycle. The name `tasks` is deliberate reclamation: the deleted engine owned execution; this owns only the callback. |
| D2 | **Completion is open but provenance-stamped** — any conversation turn, script, subagent, sweep, or operator may complete any task; `completed_by` records who/what | Matches the steering split (human-says vs system-says). The waiter reads provenance and judges what to trust — a poller script posting a GLB path and a group member posting "yep done" are both valid; the wake content tells them apart. No ACL for what is really an information-trust question. |
| D3 | **Single completion, CAS** — `UPDATE … WHERE status='pending'`; late/duplicate completions no-op and return the settled state | Scripts retry; effects replay; humans double-confirm. One winner, everyone else told the outcome. Goal-transition semantics. |
| D4 | **Complete-and-wake is an effect** (`task_settle`), emitted before the wake | The crash window between "completed" and "waiter told" must replay, not lose — the exact hole the OOM incident fell through. Idempotency per task id makes replay safe. |
| D5 | **Every task has a due** (default **+1h**, Mike 2026-09-19 — most tasks are script/subagent callbacks resolving in minutes; long waits pass an explicit due) — a wakeup backstop, cancelled on settle | Completion-based and time-based liveness are complements. Hung tasks surface as "due, still pending" wakes in the waiter, not silent stalls. |
| D6 | **Completer-side visibility** — a `tasks_block` injection ("tasks awaiting this conversation") in every dispatch path, the analogue of goals_block | The outreach goal's real trick was sitting in the target DM's context so the reply three hours later still knew it owed something. A one-shot steer forgets; the block remembers. Without it the promise is visible only to the waiter. |
| D7 | **`expected_completer` is a hint, not a gate** (session key, subagent id, bg unit name, or `script`); the boot sweep uses it for liveness checks | D2 keeps completion open; the hint exists so reconciliation knows what to check (subagent row status, unit liveness, script grace). |
| D8 | **Refs ride the task** (`refs_json`) and are offered to the completer conversation's extraction | Inherits the parented-outreach identity property (the target DM's extractor offers the plan's entities so replies ref-match back). |
| D9 | **Result is stored on the row**, not only in the wake | Late readers (`list_tasks`, check-ins, dashboard audit) see what happened; the wake can be lost and re-derived. |
| D10 | **Script-facing endpoint**: `POST /api/v1/tasks/{id}/settle` (complete/fail), token-gated on the stimulus-ingest pattern | `swans-watch.sh`, the Meshy poller, a merch-ledger watcher — processes gain a callback path with no LLM. This is the square missing from the bg-grid. |
| D11 | **Rooms are first-class waiters**: `task_register` everywhere; check-ins list the room's pending tasks with ages | Converts room polling into event-driven completion while check-ins remain the floor. Outreach auto-subscribe moves to register-time (waiter is a room → subscribe to target entity). |
| D12 | **The waiter's wake content carries a provenance header** (who completed, when, via what) rendered before the result | The waiter narrates honestly ("the poller says…" vs "David says…"), same discipline as steer provenance. |

## Data model

```sql
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,             -- task-<hex8>
    title TEXT NOT NULL,
    waiter_session TEXT NOT NULL,    -- conversation woken on settle
    payload_json TEXT NOT NULL DEFAULT '{}',  -- context for the completer
    expected_completer TEXT,         -- session/subagent id/unit name/'script' (D7)
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','completed','failed','cancelled')),
    result TEXT, error TEXT,
    completed_by TEXT, completed_at TEXT,
    due TEXT NOT NULL,               -- ISO; D5 backstop
    source_goal_id TEXT,             -- optional: registering room's goal
    refs_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX idx_tasks_waiter ON tasks(waiter_session, status);
CREATE INDEX idx_tasks_completer ON tasks(expected_completer, status);
CREATE INDEX idx_tasks_due ON tasks(status, due);
```

Lifecycle: `pending → completed | failed | cancelled` (CAS both directions).
Registration writes the row + the due wakeup (kind `task_due`, payload
carrying task id — cancelled by the settle effect, the
`cancel_for_routine` payload-match pattern).

## Mechanics

**Register** (`task_register(title, instruction, completer_session="", due="", refs=[])`):
insert row; schedule due-wakeup; if `completer_session` set, steer it with
the instruction + task id (the delegation half); if waiter is a goal room,
auto-subscribe engagement (D11).

**Settle** (`task_complete(task_id, result)` / `task_fail(task_id, error)`,
the endpoint, or the sweep): CAS the row; emit `task_settle` effect; the
executor cancels the due-wakeup and wakes the waiter with the D12 header +
result. Cancel: waiter or operator may `task_cancel(reason)`.

**Completer context**: `tasks_block(session_key)` lists pending tasks where
`expected_completer = session`, injected beside goals_block on the WA path
and the generic wake path (rooms included). The block's copy instructs:
complete with `task_complete` when the human's reply or the job's outcome
gives you the answer; quote the task id.

**Boot sweep** (heartbeat, hourly beside GoalRoomHygieneTask):
- pending + `expected_completer` is a dead subagent row, a dead bg unit, or
  `script` past grace (default 2× due) → `task_fail("completer died at …")`.
- pending + due long past (backstop fired, still nothing) → flagged in the
  waiter's next check-in as overdue (not force-failed — the waiter decides).

## Worked example — the gnome, done right

Room turns: `task_register("Meshy refine smith-gnome-v3", instruction=…,
completer="script", due=+45m, refs=[file-bob-liebherr…])` → spawns the
poller with the task id. Poller POSTs `/tasks/<id>/settle` with the GLB path
when Meshy finishes. The OOM kills the poller anyway: the sweep sees
`completer=script` past grace → `task_fail("completer died")` → the room
wakes, knows the submitted job id (payload), queries Meshy before
resubmitting (double-spend guard), re-registers. Every failure became a
message.

## Migration phases (each independently green; Mike wants speed)

**Phase 0 — prerequisite:** fix the eval harness tool-loop (control case
`tool_calling_update_agenda` makes zero calls; the outreach flip needs
replay verification).

**Phase 1 — registry live, nothing changes behaviorally.** Table, repo,
tools (register/complete/fail/cancel/list), endpoint, due-backstops,
`tasks_block`, boot sweep, room check-in listing. Tests for: CAS
single-settle, effect replay (kill between settle and wake), sweep
reconciliation, endpoint auth, D12 header. Kill switch: `BOB_TASKS=off`
hides the tools/injection; table keeps rows.

**Phase 2 — outreach recomposes.** `send_whatsapp_to_contact` internally
becomes steer + `task_register` (waiter=caller, completer=target DM,
due=+24h, refs inherited as today). `finish_outreach` becomes a
`task_complete` shim (same name, same contract — no prompt churn in DMs).
`goals_block`'s outreach special-case is replaced by `tasks_block`.
Auto-subscribe moves to register-time. Flag `BOB_OUTREACH_VIA_TASKS`
default **on** (rollback = off). Delete outreach-goal minting after one
clean week.

**Phase 3 — subagent wrappers become tasks.** `create_subagent` registers
the task; `_run_subagent` settles via effects (restart-safe — the incident
class dies); boot sweep covers the race the effects can't.

**Phase 4 — legacy goal machinery retires.** Drain check: no active
outreach/subagent goals, all deliberative kinds roomed. Then delete: the
reviser (`goal_state_service` and its executor), claim-router matching +
probe + `memory_routing_log` (keep the emission side — it is the sensation
backbone), GoalReviewTask's reviser path, the `BOB_GOAL_ROOMS` fallback
scaffolding, and the `_legacy_goal_path` test fixtures. `goals` table
slims conceptually (rooms own deliberation; no schema change needed).

## Testing & verification

- CAS: concurrent double-complete → one winner, second told settled state.
- Durability: settle effect emitted, executor killed before wake → replay
  wakes exactly once; due-wakeup cancelled.
- Sweep: dead-subagent/dead-unit/script-grace each produce `task_fail` with
  reason; overdue-but-possible tasks surface in check-in, not force-failed.
- Endpoint: token gate; unknown id; settle-after-cancel no-ops.
- Completer visibility: `tasks_block` renders for the expected completer
  only; the reply turn (fresh dispatch, hours later) still sees it.
- Outreach parity (Phase 2): golden tests of the current outreach flow
  re-run against the composed version — same wakes, same auto-subscribe,
  same refs inheritance. This is why Phase 0 gates.
- Room integration: registered task + completer settle → room woken with
  D12 header; check-in lists pending with ages.
- Kill switches: `BOB_TASKS=off`, `BOB_OUTREACH_VIA_TASKS=off` both leave
  prior behavior intact.

## Decision points — settled by Mike 2026-09-19

1. **Endpoint token**: separate `BOB_TASK_TOKEN` (blast-radius isolation
   from the stimulus secret). Settled.
2. **Human completion trust**: as recommended — open completion with
   provenance, waiter judges; no gate. Settled.
3. **Default due = 1 HOUR** (Mike's ruling — sharper than outreach's 24h;
   most tasks are script/subagent callbacks that resolve in minutes. Long
   waits pass an explicit `due`.) Settled.
4. **No drain period for Phase 4** — the legacy goals are dead; delete the
   machinery and clean up the straggler actives (orphaned wrappers, parked
   tz-greetings) in the same change. Phase 4 folds into the build rather
   than trailing a week. Settled.
5. **Task short-ids in chat**: yes — humans may reference tasks by short id
   ("cancel task a3f2"); tools already accept it. Settled.
6. **Dream plans seed goals (rooms)** — `dream_plans.task_id` stays
   goal-linked; plans produce goals, not tasks. Settled.
