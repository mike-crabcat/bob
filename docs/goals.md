# Goals

Goals are how work that takes more than one turn gets done. A goal is a
conversation with a memory, a wallet of tasks, and a rule for when it
speaks next — not a cron job, not a wish list. Everything below is how
they actually behave on this system.

## What a goal is

Four primitives, one job each:

- **The room** — a private utility conversation that works exactly one
  goal. The charter pasted into it is its behaviour spec; its history is
  its working memory. Work is deliberated here, never in the group or DM
  that asked for it.
- **The state block** — the goal's living worksheet (`room_state`):
  plan, known facts (append-only evidence), open questions, next steps,
  the **strategies tree** (hypotheses with verdicts — pruned branches
  stay, so "why we abandoned approach B" stays answerable), and the
  **artefact registry** (every file the goal produced, with paths).
- **Tasks** — the universal branch. Experiments, build steps,
  confirmations from humans: all are promises between conversations.
  Completing a task wakes the conversation waiting on it.
- **The continuation** — how the goal decides when to work next (below).

The goal row in the database is the lifecycle registry (status, budget
counters, deadline, owner). The room owns the thinking; the row owns the
bookkeeping.

## Kinds

`kind` picks the charter playbook — the discipline, not the machinery.
One engine runs them all:

- **task** — general single-objective work
- **research** — investigate, evaluate, recommend. Branch, test, prune;
  the tree of considered-and-rejected approaches is a first-class outcome
- **build** — artefacts with proofs. Nothing is done until it passes its
  declared verification. Code may run to green tests on a branch;
  merging/deploying is requested from the owner, never done in-goal
- **sales_target** — revenue goals. Bias to action, parallel branches,
  group pushes requested via the origin, ledger is the proof
- **negotiate** / **event_plan** — human confirmations and arrangements.
  Every confirmation is a task pointed at the conversation that talks to
  that person; silence gets a follow-up ladder (re-DM → email → call)
- **performance** — improve the system you are running (e.g. trading):
  measure, attribute, branch more of what pays, prune what loses; the
  rules file is operator-owned, changes are requested

Kinds are canonicalised at creation (`event` → `event_plan`, `sales` →
`sales_target`); unknown kinds are refused with the valid list.

## The loop: when a goal works

Every room turn ends with a **continuation declaration**:

- `goal_continue_now(reason)` — chain the next round immediately (capped;
  overuse forces a cooldown)
- `goal_wait(minutes or until, reason)` — pause precisely; typed values
  only, never prose
- **nothing, when branches are pending** — task completions wake the room
- `complete_goal` / declaring blocked — the series ends

The declaration is a hint over a guaranteed floor: if a turn forgets to
declare, pending tasks drive the next wake, and a **dead-man heartbeat**
(a slow recurring check that skips itself when the room spoke recently)
means no goal ever goes silent by accident. Budget is counted in
**rounds** (one per turn); at ~70% the frames warn, at 100% the next
round is a **terminal frame** — complete with findings, declare blocked,
or request renewal from the origin. Renewal is owner-only.

## Where the goal's voice goes

The **origin conversation** — wherever the goal was asked for — receives
the goal's reports: completions, blocked declarations, renewal requests,
and shared reveals. The dashboard is for following along; it never
pushes. The owner (the person who commissioned it) is a participant, not
a bystander: ask them for their own decisions, and treat anything they
relay about someone else as authoritative.

## The channel rules

- **Per-person work — offers, chases, confirmations, approvals — happens
  in that person's DM.** A task's completer is the DM conversation; their
  reply settles it and wakes the room. Never chase individuals through a
  group. Registration refuses room→group delegation outright.
- **Shared artefacts are group content.** When the goal produces
  something the group is waiting to see — a figurine, a mockup, a
  lineup — post it as it lands. Reveals belong to the audience.
- **Goal-scoped work is handed to the room.** If someone asks in a chat
  "render X / build Y" and it belongs to an active goal, register a task
  for the room — do not run the pipeline in the chat. Chats deliver
  results; rooms run pipelines.

## Artefacts and the goal directory

Every goal gets `goals/<id8>/` in the workspace (created for it). Write
every file the goal produces there, and record each in the state block
with `goal_artefact(path, what)` as it lands. Closure evidence must
enumerate all artefacts — inputs and final — and cite the delivery
receipt. A result describing files nobody can find is not evidence.

## Waiting and background work

Long waits never hold a turn: `run_bg_process` runs the work and **wakes
the conversation when it finishes** — no sleep timer on top (a finished
job once sat an extra hour because of one). The pattern: register what
you'll do on wake (task or goal state) → start the background process →
end the turn. Short sleeps (≤10s) are fine inline; the sleep tool
refuses longer ones with this exact guidance.

## Failure modes this design exists to prevent

- **Clock-driven check-ins** — replaced by the continuation contract
- **Silent stalls** — dead-man heartbeat + terminal frames; budgets
  expire loudly, never quietly
- **Duplicate chat rows / phantom sends** — every external action is an
  effect, delivered once, receipt-citable
- **Zombie rooms** — settling a goal disables its room; dead goals never
  run again
- **Group spam** — per-person work can't be delegated to groups; unmet
  tasks get task_failed immediately, not retried into repetition
- **Lost work** — artefacts registered, evidence append-only, verdicts
  kept even for failures

## For operators

- Dashboard: **Goals** — list with next-run, budget burn, branch counts;
  drill into a goal for state, strategies tree, branches, and the
  wake/turn timeline.
- Kill switch: `BOB_GOAL_LOOP=off` reverts rooms to fixed check-ins.
- Deep design: `goal-execution-plan.md` and `goal-rooms-plan.md` in the
  platform repo docs (not in this workspace).
