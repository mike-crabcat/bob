# Backburner — detaching slow WhatsApp turns to the background

Status: **implemented + live 2026-08-30** (mode `full`, all DM conversations) ·
Written 2026-08-30 (design discussion with Mike, same day). Implementation:
`services/backburner.py` + DispatchRunner watchdog + bridge capture/hold +
kill deltas + restart recovery; tests in `tests/services/test_backburner.py`.
Motivation: since the glm-5.3-flash switch, many WHATSAPP_INCOMING turns run
well past 30s. A slow turn holds the session the whole time: the contact
stares at silence, double-texts queue behind the lock, and Bob looks dead
precisely when he's working hardest.

## Goal

When a WHATSAPP_INCOMING turn exceeds a threshold (~30s), Bob:

1. sends a **holding ack** — a dynamically formed "still working on that,
   I'll get back to you" in his own voice, aware of what the turn is about,
2. **detaches**: releases the session so new messages get normal turns,
3. keeps the original work running **on the backburner** as a tracked
   background task, visible to later turns (so they don't compete with it)
   and cancellable by the user,
4. delivers the eventual result as a *new* turn, in Bob's voice, with full
   context of whatever was said meanwhile.

## Non-goals

- **No lock restructure for all channels.** Email/group/routine dispatches
  keep today's fully-serialised semantics. Detach is a WHATSAPP_INCOMING
  (v1: DM-only) behaviour layered inside DispatchRunner.
- **No replay/restart of the in-flight work.** The running `chat_with_tools`
  loop continues exactly where it is — restarting it would re-execute tools
  with side effects (duplicate emails, rewritten files).
- **No undo.** Kill means "stop spending", not "rollback": effects already
  emitted to the outbox are durable and will deliver.
- **No streaming partial replies** to the contact mid-turn.

## Current architecture (verified 2026-08-30)

Three layers serialise a session while a turn runs:

| Layer | Where | Behaviour while held |
|---|---|---|
| `AttentionCoordinator._dispatching` | `services/attention/coordinator.py:80` | new stimuli buffer (`submit()` returns early); `finally` sweep re-arms leftovers after the dispatch returns |
| `SessionDispatchGate` asyncio lock | `services/dispatch_runner.py:139` | held across the *entire* `chat_with_tools` loop, i.e. every tool call |
| `TurnRepository.claim` | `repositories/turns.py` | durable single-running-turn per conversation, 300s lease, advisory (a `None` claim is tolerated) |

Mid-turn arrivals sit as pending rows in `session_messages` until the turn
ends. So a 4-minute turn = 4 minutes of silence even if the user double-texts.

Machinery this design rides on (all exists today):

- **In-flight transcript is durably inspectable.** `chat_with_tools` writes an
  `llm_call_log` row with `status='running'` and updates `messages_json` on
  every iteration (`services/llm_dispatch.py:577-601`).
- **Late-result delivery has a hardened path.** `wake_conversation`
  (`services/wake_service.py`) stores content as a pending user message and
  runs a full inbound turn. Subagent completion already uses exactly this
  (`subagent_service.py:449-502`).
- **Later turns already see background work.** `ContextAssembler.goals_block`
  (`services/context_assembler.py:118`) injects each active goal the
  conversation holds into every dispatch's system prompt. `create_goal`
  registers the parent as a holder (`goal_service.py:62-64`), and
  `goals_held_by` joins any-role (`repositories/goals.py:100-112`) — so a
  detached turn's goal shows up automatically, no new injection point.
- **Cheap mid-pipeline LLM calls have precedent**: the Tier-2 probe
  (`services/attention/tier2.py`) — failure policy: any error falls through,
  never blocks the dispatch.
- **Out-of-band sends** go through the effects outbox with idempotency keys
  (same as `_send_whatsapp_message`).

## Settled design decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **Detach inside `DispatchRunner.run`; don't restructure the lock.** Wrap `chat_with_tools` in a task + `asyncio.wait(timeout=DETACH_AFTER)`. | Changing lock semantics for every channel is a big blast radius; a bounded change inside the runner gets the same effect. |
| D2 | **The detached run never delivers directly after detach.** Its send tool flips to *capture mode*; captured text becomes the task result, delivered later via goal settle → `wake_conversation`. | Ordering and lock discipline stay intact by construction: the result lands as a proper new turn, in Bob's voice, with full context. The background run becomes a second-class citizen that no longer owns the conversation. |
| D3 | **Register, don't replay.** The in-flight loop is registered as a `subagents` row (`agent_type='detached_turn'`) + goal; the asyncio.Task is never restarted. | Restarting re-executes side-effecting tools. Registration buys the dashboard, `check_subagent`/`kill_subagent`, goal visibility, and settle/wake for free. |
| D4 | **History is written at detach time, once.** Whatever `sent_texts` has accumulated is recorded while still inside the gate; post-detach sends are captured only and the detached path never writes conversation history again. | Eliminates concurrent-history races between the background task and new turns. |
| D5 | **Holding ack is skipped if the turn already sent a message** (`message_was_sent`). | If Bob already said "on it!", a holding ack is noise. |
| D6 | **DMs only in v1** (gate on `call_category == "whatsapp_incoming"` plus chat_kind check). *Widened to groups at deploy, 2026-08-30: Mike asked for all conversations, and the first half-hour of live traffic was 100% group turns (7 slow, worst 215s) — the DM gate left the feature idle. Same day: **human-stimulus gate added** — turns claimed solely by `wake_nudge`/`routine` provenances never detach (holding acks for them are uninvited speech, and detaching relay turns amplifies: task → relay nudge → slow relay turn → task → …; observed in the AI doom group).* | Holding acks in groups are noise; group member-change turns are a different category anyway. |
| D7 | **Probe failure never blocks detach.** Any inspector error/timeout → template summary + template holding text. | Same contract as the Tier-2 probe: infrastructure must not cause silence. |
| D8 | **Completion of a detached task wakes via the goal; kill does not wake.** | Completion: user needs the result. Kill: the user is mid-conversation and the cancelling turn confirms inline (`wake_origin=False` precedent, `subagent_service.py:416-419`). |
| D9 | **Naming**: feature = *Backburner*; code stays mechanical — `detach`/`detached_turn`/`detach_probe`/holding ack. See naming table below. | Logs read precisely; dashboards read human. `defer` is rejected (`occupancy.defer` already means call-live deferral), as are `callback`/`yield`. |

## Mechanics

### Detach sequence (inside `DispatchRunner.run`, after `asyncio.wait` times out)

Ordered — b/c/d/f/g happen while the gate is still held, then `run()` returns:

- **a. Probe.** Read the running `llm_call_log` row (session + status
  `running` + category `whatsapp_incoming`); call `detach_probe` (spec
  below). Timeboxed; on any failure use templates.
- **b. Record history** for `sent_texts` accumulated so far (D4).
- **c. `TurnRepository.complete(turn_id)`** — the turn is *answered* (by the
  holding ack); the claim frees for the next turn.
- **d. Register**: `subagents` row (`agent_type='detached_turn'`,
  parent = session, task = summary) + `create_goal(conversation_id=<subagent
  session>, origin_conversation_id=<parent>, kind='subagent',
  external_ref=<subagent_id>)`, objective = probe summary, strategy fields
  populated from the tool trace so far (`Known:` from tool results, `Next:`
  from the pending call — mechanical, no extra LLM call). Register the
  asyncio.Task in the running-tasks registry so `kill_subagent` finds it.
- **e. Flip the send tool to capture mode** (shared `detached` flag the
  handler checks; captured texts become the task result; tool result tells
  the model its sends are captured and will be relayed).
- **f. Send the holding ack** via the effects outbox, idempotency key
  `whatsapp_send:hold:{dispatch_id}` — unless D5 skips it.
- **g. `return` from `run()`** — gate releases, `_dispatching` clears, and
  the coordinator's existing leftover sweep re-arms pending messages into a
  fresh turn that sees the goal via `goals_block`. Publish `spec.event` here
  (the message was received and handled), not at completion.

### Background completion path (supervisor coroutine, strong ref — the `wake_service._pending_dispatches` pattern; remember the 2026-08-25 task-GC incident)

- **On finish**: store result on the subagent row (result + captured sends),
  `settle_goal(completed, result="[Background task <short>] … — relay to the
  user with a summary")` → wake fires → delivery turn acquires the lock
  properly and speaks in Bob's voice. Goal leaves `goals_block`.
- **On CancelledError** (user kill): mark the subagent `killed`, settle the
  goal `cancelled`, **no wake** (D8). Snapshot the capture buffer into the
  subagent's `result` column so "whatever happened with that?" is answerable
  later.
- **On other exceptions**: settle goal `failed` + wake with the failure text,
  so Bob tells the contact plainly. (Quota failure here does NOT restore
  pending — the original messages were already answered by the holding ack;
  pinned asymmetry vs the in-turn quota path in `dispatch_runner.py:199-206`.)

### `detach_probe` — the extra LLM call

| | |
|---|---|
| `call_category` | `detach_probe` (joins the existing `_probe` family: `attention_probe`, `claim_router_probe`, `outreach_probe`) |
| Model | `settings.patience.model` (the Tier-2 probe model — already configured, already cheap) |
| Input | running row's `messages_json` (the user message + tool calls/results so far), capped (reuse `_cap_item`-style truncation, last-N items); never the raw system prompt. **Live fix 2026-08-30:** the messages array carries prior-turn history *including* prior-turn tool items — the probe transcript is now cut at the last user message (trigger = its content, work = only items after it). Found live: a group turn 32s in with zero tool calls of its own showed the probe a full tail of the previous turns' merch/257 work; the summary survived on the user message alone, but the probe could have summarised the wrong turn. |
| Output | JSON `{summary, holding_text}` — summary one or two lines ("what this turn is doing"); holding ack short, in Bob's voice, honest |
| Timebox | ~8s `asyncio.wait`; on timeout/error → templates: summary "working on the last request" / ack "Still working on that — I'll get back to you shortly." |
| Locks | none — plain `chat()` call, no session gate, no claims |

### Cancellation (user says "never mind, drop that")

`kill_subagent` already does nearly the right thing
(`subagent_service.py:380`): scoped to own-conversation tasks (`:386-388`),
cancels the registered task (`:389-391`), settles the goal without waking
(`:416-419`). Deltas needed:

1. `detached_turn` tasks registered in the running-tasks registry (done at
   detach, step d).
2. Completion wrapper swallows CancelledError without waking (above).
3. **Already-finished guard**: if status is `completed`/`failed`, return
   `{"ok": false, "error": "already finished"}` instead of marking a finished
   task `killed` — this race is *likely* ("cancel it" arriving just as a
   3-minute task finishes), and the model should relay the result instead.
4. **Log-row reason**: a cancelled `chat_with_tools` records
   `error_message="Cancelled — server restart"` (`llm_dispatch.py:664`) —
   hardcoded, wrong for user kills, and the dashboard would show every user
   cancel as a restart. Distinguish.
5. **ID plumbing**: `goals_block` renders the goal id but `kill_subagent`
   wants the subagent id. Render the subagent short-id in the detached goal's
   block (or accept a goal id in kill), and make "Subagent not found"
   responses hint "call list_subagents" — the model invents/confuses ids
   (known failure mode), so let it self-recover.

Known limit: **no pre-detach cancellation** — before the watchdog fires, the
turn holds the lock; a "cancel" message just buffers as pending. Same as
today's behaviour; state it in docs so nobody expects sub-30s cancel.

### What subsequent turns see

While the task runs, every dispatch's system prompt carries (via
`goals_block`):

```
### <probe summary> (subagent, id goal-xxxx)
Known: <from tool results at detach>
Next: <pending tool call at detach>
```

plus active channels: `check_subagent`/`list_subagents` tools.
**Freshness gap**: the goal is a snapshot written at detach. Mitigation (in
preference order): make `check_subagent` live for `detached_turn` — read the
current running `llm_call_log` row and return the latest tool-activity
summary on demand; richer detach-time strategy (done, D-step d); periodic
re-summarisation rejected (cost). Lifecycle transitions cleanly: running →
goal in prompt; completed → goal settled, wake message is the delivery
turn's stimulus; killed → settled, gone.

## Race conditions & failure modes

| Risk | Mitigation |
|---|---|
| Turn finishes at 30.1s → spurious holding ack | `asyncio.wait`, never sleep-then-check; the watchdog races the *task*, not a clock |
| Background task GC'd mid-flight | strong-ref set, supervisor coroutine (wake_service pattern) |
| Server restart orphans active goals (task dead, goal forever in every prompt) | startup sweep: `detached_turn` subagents still `running` → mark failed + optionally wake "I lost that when I restarted — want me to redo it?" (mirrors `resume_pending_sessions`) — **verified live 2026-08-30: 2 orphaned goals settled at boot, conversations re-armed by the +10s sweep** |
| Two detached tasks + goals crowd the prompt | `goals_held_by` caps at 5, newest first — fine at realistic volumes; note for later |
| Probe hangs (flash is slow) | 8s timebox + template fallback (D7) |
| Double holding ack | outbox idempotency key `whatsapp_send:hold:{dispatch_id}` |
| Kill mid-tool-call | cancel lands at next await; effects already emitted deliver. Bob should say "stopped it — though the X it already sent is out", not promise rollback |
| Runaway detached task burns quota | wall-clock cap on detached tasks (`MAX_RUN_S`, ~10 min): supervisor cancels + settles failed at the deadline |

## Adjacent bugs found while reading (fix independently, before/with phase 3)

1. **`heartbeat_lease` is never called.** A turn >300s has its lease expire
   mid-run; the next `claim()` then fails the "zombie" turn and releases its
   events *while it's still executing* (`repositories/turns.py:70-78`). With
   glm-5.3-flash this is no longer hypothetical. Fix: heartbeat from the
   dispatch loop (per tool iteration is natural). *(Fixed with the deploy.)*
2. **No wall-clock limit on main dispatches** — `time_limit_seconds` is only
   used by `routine_service`. A hung turn holds the session lock forever.
   *(Fixed with the deploy: whatsapp_incoming turns get max_run_seconds.)*
3. **Restart mid-LLM permanently consumed the dying turn's claimed messages**
   (found live 2026-08-30: a deploy restart ate an in-flight question — the
   "10-minute delay" incident; the crash-recovery sweep only re-arms
   *undispatched* messages). *(Fixed post-deploy: boot sweep restores claims
   via `restore_messages_for_turn` before failing zombie turns —
   `tests/services/test_turn_boot_recovery.py`. Residual variant: a turn
   whose lease already expired and was failed by a later claim before boot
   is not restored — its events release but its dispatched flags stay set.)*

## Rollout (careful-rollouts convention)

- **Phase 0 — measure** (no code): dashboard over `llm_call_log` — slow-turn
  frequency/duration by category. Confirms the 30s threshold.
- **Phase 1 — shadow**: watchdog + probe run, results logged only
  (`journalctl` + llm_call_log `detach_probe` rows). Verify summary and
  holding-text quality on real slow turns.
- **Phase 2 — holding ack only**: send it, keep waiting on the turn (no
  detach). Near-zero risk; kills the worst UX (silent 90s) on its own.
- **Phase 3 — full detach**, seeded to Mike's DM via session allowlist
  before global. Requires: heartbeat fix + restart sweep + already-finished
  kill guard.
- **Phase 4 — cancellation polish + `check_subagent` live read.**

Settings (env, matching `BOB_ATTENTION_ALWAYS_ACT` style):
`BOB_BACKBURNER_MODE=off|shadow|hold|full`, `BOB_BACKBURNER_DETACH_AFTER_S=30`,
`BOB_BACKBURNER_PROBE_TIMEOUT_S=8`, `BOB_BACKBURNER_MAX_RUN_S=600`,
`BOB_BACKBURNER_SESSIONS=<allowlist>`.

### Verification per phase

- Phase 1: sample ≥10 real slow turns; summaries accurate; probe latency
  p95 under ~5s; zero dispatches blocked by probe failure.
- Phase 2: holding acks read naturally (Mike's judgement); no ack when the
  turn already sent; no duplicate acks (outbox keys).
- Phase 3: on a forced-slow turn — ack arrives ~35s; double-text claimed by a
  new turn ≤5s later that does *not* redo work; result delivered as a turn
  with correct ordering; kill works incl. already-finished race; restart
  sweep clears orphan goals; turn lease no longer expires under a 6-min turn.
- Unit tests alongside (`tests/services/`, next to `test_dispatch_rescue_gating.py`):
  watchdog boundary (task completing at/after timeout), detach idempotency,
  capture-mode sends never reach the outbox, kill of finished task, restart
  sweep.

## Open questions

1. Fixed 30s, or only detach when pending messages exist / after a second
   softer threshold? (v1: fixed.)
2. Extend to groups later? (v1: DM-only, D6.)
3. Bridge `composing` presence: the bridge only *receives* `chat_presence`
   today — if the Go bridge can send a typing indicator, showing "typing…"
   for slow turns is a zero-content UX win that pairs with the holding ack.
   Needs a bridge protocol check.
4. Should the delivery wake also fire when the result is empty/NO_REPLY-ish
   (task finished, nothing to say)? Lean: settle quietly, no wake.

## Naming

| Thing | Name |
|---|---|
| Feature / plan / dashboard section | **Backburner** |
| State transition / verb in code | `detach` — turn is `detached` |
| Subagent `agent_type` | `detached_turn` |
| Extra LLM call (`call_category`) | `detach_probe` |
| The outbound "still working on it" | holding ack (`holding_text` in probe output) |
| Result delivery | the wake path (existing name) |
| Watchdog / threshold setting | detach watchdog / `BOB_BACKBURNER_DETACH_AFTER_S` |
| Rejected | *deferred* (`occupancy.defer` collision), *callback*, *yield* |
