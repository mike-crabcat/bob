# Dream suggestions — Phase 4 of the commitments plan

Built 2026-10-05 (`docs/commitments-plan.md`, Phase 4); the old plan
tools, plans prompt and offer-tasks were deleted 2026-10-06.

## The shape

Two objects, one each side of the human:

- **`dream_plans` — the dream's private notebook.** The dream pass still
  drafts, approves, reviews and expires plans exactly as before. Chat turns
  never see them: plan tools and the plans prompt are gone.
- **Suggestion goals — the public offer.** When a plan is announced in a
  chat, announce writes one `goals` row: `kind='suggestion'`, id `sug-…`,
  status `suggested`, `origin_conversation_id` = the chat, `external_ref` =
  the plan id. No offer-task.

**Invariant: dreams never create active goals.** A suggestion has no wakes,
room or loop until someone in the chat says yes.

## Lifecycle

```
dream plan ──announce──► suggested  (listed under "Suggested" in the chat's Work block)
                            ├─ accept_suggestion ──► accepted; a real goal is created, plan → actioned
                            ├─ close_goal(cancelled) ──► declined; plan → dismissed
                            └─ dream closes the plan ──► expired (dismissed → declined)
```

- Only the chat it was offered in can accept or decline.
- Settles are CAS from `suggested`; a repeat accept answers "already accepted".
- `suggested` is outside every sweeper's `status='active'` scan, so the
  scheduler, goal loop, rooms, reviser and outreach never touch it.

## Code

- `dream/announce.py` — v2 branch: `GoalRepository.create_suggestion` per
  announced plan (idempotent per plan).
- `dream/store.py` — `set_plan_status` terminal → settle the open suggestion.
- `goal_work_tools.py` — `accept_suggestion`; `close_goal` on a suggestion
  declines.
- `context_assembler.py` — *Suggested* section of the Work block.
- Dashboard Work page lists pending suggestions.

## Migration

None. The only announced open plans (2, both from August) are stale and stay
dormant in `dream_plans`; new announcements write suggestions.

## Tests

`tests/services/test_goal_suggestions.py` (listing + sweeper invisibility,
accept, decline, offering-chat-only, expiry) and
`test_goal_rooms.py::test_dream_announcement_creates_suggestion`.

## Open question

The workspace `TODO.md` convention: track suggestions and active goals, or
retire it?
