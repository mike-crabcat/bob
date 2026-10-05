---
name: goal-craft
description: How to write goals that run well — objectives that name the artefact and its proof, outcome vs promise, splitting work into children, approval checkpoints, and branch-shaped work. Read BEFORE add_goal for any non-trivial goal (research, build, negotiate, events, anything covering several people or items).
trigger: before calling add_goal for an outcome — especially when the ask is fuzzy ("look into X", "make Y work", "sort out Z with P"), when it covers several people or items ("for each of us", "for the 9 of us"), when a step costs money, when a goal feels like it contains two goals, or when writing the first state block of a new goal room
---

# goal-craft

Goals are loops now: a room works the goal in rounds, every round ends with
a continuation, child promises carry the work, and a strategies tree
records what was considered. A goal runs exactly as well as it is written.

## The three rules

1. **The objective names the artefact AND its proof.** "Done" must be
   checkable by evidence, not vibes. If you can't say what proves it, the
   goal isn't ready to create.
2. **One goal = one objective.** Two audiences, two outcomes, two tempos →
   two separate outcome goals. Parts of ONE objective are children, not
   siblings (see "Splitting the work" below). An omnibus goal drifts and
   stalls — that failure has happened repeatedly.
3. **Branches over plans.** Unknowns are resolved by small testable
   branches (strategy_open → child promise → strategy_result), not by a
   grand plan written up front. The tree of considered-and-pruned
   approaches is a first-class outcome.

## Before add_goal

Answer, in the objective or state block:
- What exists when this is done? (artefact / answer / arrangement)
- What proves it? (test green, reply quoted, booking reference, screenshot)
- OUTCOME or PROMISE? `profile="outcome"` is multi-step work toward an
  objective — it gets its own room and runs in rounds. `profile="promise"`
  is one thing owed by a single party (you, or another conversation via
  delegate_goal) — no room. Almost every goal you create is an outcome.
- Label: a short free-text name for the work's shape — it picks playbook
  guidance and never decides whether the goal gets a room. Familiar ones:
  "see if we can X" → research; "make X work" → build; "get P to agree to
  X" → negotiate; "sell/earn $N from ..." → sales_target (bias to action,
  parallel branches, group pushes requested via the origin); an event →
  event_plan.
- Deadline real? Only set one when the world imposes it.
- WHO owns it? The goal room acts with its owner's reach — their trust
  scopes what the room may see (e.g. group rosters). In a DM the owner is
  the person you're talking to (automatic). In a GROUP, ask who owns the
  goal before creating it (unless they already said) and pass them as
  `owner=` on add_goal. Don't guess and don't default to the last speaker.

## Splitting the work

- **Children, not siblings, for parts of one objective.** "A figurine for
  each of the 9 of us" is ONE outcome (the set) with 9 child promises —
  not 9 goals. Siblings are for genuinely separate objectives.
- **Fan-out in one call.** Create all the per-person/per-item children in
  the SAME add_goal call via `children=[…]`, never one call each — long
  runs of near-identical calls skip and duplicate items.
- **Name a completer when someone else owes it.** A child that needs a
  person's answer goes to that PERSON'S DM conversation (never a group —
  a group can only reply in the group).
- **Checkpoint before paid or irreversible steps.** Before 3D/image
  generation, orders, bookings or anything that spends money or can't be
  undone, add a child whose completer is the owner's conversation asking
  them to approve — and wait for it. Showing concepts first is cheaper than
  regenerating models.

## Example files — read the one matching the work

- `examples/research.md` — investigation goals, multiple strategies
- `examples/build.md` — technical artefacts, verification, autonomy limits
- `examples/negotiate.md` — human counterparts, confirmations, follow-up
  ladders
- `examples/fanout.md` — one objective across many people/items, with an
  approval checkpoint before paid work

Each has a bad→good pair with the reasoning. Steal the good shapes.

## During the run (charter reminders live in the room)

- End every round with a continuation (goal_continue_now / goal_wait /
  nothing-when-children-pending — their results wake you).
- Record every strategy outcome — pruned-with-reason is as valuable as won.
- Close children with close_goal as they finish (completed / failed with
  the reason); close the outcome only with evidence; report to the ORIGIN
  conversation; budget renewal is requested there too.
- Artefacts live in the goal's directory (goals/<id8>/) and are
  recorded in the state block (goal_artefact) as they land — a goal's
  deliverables must be findable from its record alone.
