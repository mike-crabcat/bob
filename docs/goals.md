# Goals

Goals are how Bob keeps track of everything he owes, is doing, or has
offered — anything that outlives a single turn. One table (`goals`), three
shapes, six tools, one prompt block. Everything below is how it actually
behaves on this system (current as of 2026-10-06).

## The three shapes

Every row is one of these, told apart by `kind` / `profile`:

| Shape | What it is | Statuses | Gets a room? |
|---|---|---|---|
| **Outcome** (`profile='outcome'`) | A result to reach over many steps — "make $200 in merch sales", "a printable figurine for each member" | `active` → `completed` / `failed` / `cancelled` | Yes |
| **Promise** (`kind='promise'`) | One thing owed, by one party — "send David the quote", "get Chris's office days" | `pending` → `completed` / `failed` / `cancelled` | No |
| **Suggestion** (`kind='suggestion'`) | A dream offer, dormant until someone says yes | `suggested` → `accepted` / `declined` / `expired` | No |

- **Outcomes** are the goals in the everyday sense. `kind` is a free label
  (`build`, `research`, `event_plan`, `sales_target`…) that picks the
  room's playbook; it is advisory, never refused.
- **Promises** have a **waiter** (the conversation counting on it,
  `origin_conversation_id`) and a **completer** (`completer`: itself,
  another conversation's session key, a subagent id, or a script). Closing
  is once-only; the close wakes the waiter with the result. Each promise
  has a due-time backstop (default 1h; 24h for an outcome's children and
  for outreach). A promise can stand alone or hang under an outcome
  (`source_goal_id`) as one of its pieces. Ids look like `prm-xxxxxxxx`
  (older ones `task-xxxxxxxx`; both resolve, short suffixes too).
- **Suggestions** are written when a dream announces an offer in a chat.
  `accept_suggestion` creates a real outcome goal; `close_goal` declines;
  the dream closing its plan expires the suggestion. Only the chat it was
  offered in can act on it. The dream's own drafts (`dream_plans`) are its
  private notebook — chat turns never see them.

Promises and suggestions use different status words from outcomes on
purpose: every goal sweeper (scheduler, loop, reviser, rooms) scans
`status='active'` and so never touches them.

**Not goals:** execution records (a background turn, a script, a subagent
run) live in `runs`; routines live in `routines`. Neither is a commitment.

## The tools

Six, available wherever conversation turns run:

| Tool | Does |
|---|---|
| `add_goal(text, profile, label, completer, instruction, due_minutes, deadline, parent_goal_id, owner, children)` | Record something owed. `profile="outcome"` (default) opens a room; `profile="promise"` records one thing owed. `children` (JSON array) adds one promise per person/item in a single call — on a new outcome, or under an existing one via `parent_goal_id` |
| `close_goal(goal_id, outcome, result)` | Close it: `completed` / `failed` / `cancelled`. Closing something already closed is a harmless no-op |
| `list_goals()` | What this conversation holds, is owed, and owes |
| `delegate_goal(text, to, instruction, due_minutes)` | Ask another conversation to do something: records a promise it owes you and wakes it now. From a room, it files under the room's goal |
| `schedule_goal(goal_id, …)` | Schedule a future wake for a goal (full access only) |
| `accept_suggestion(goal_id, owner)` | Turn a dream offer into a real goal |

Plus `update_goal` / `update_goal_state` (progress and the state worksheet;
`expected_version` is optional) and goal templates.

**Access** follows trust: trusted conversations get everything; untrusted
groups can create (with a pinned owner) and record promises; untrusted DMs
can record promises only. With no `owner` given, the goal's **principal**
is the person being talked to (the DM contact, or the latest human
speaker in a group).

**Rules the tools enforce**
- Only the conversation a promise is **owed by** can mark it completed.
  Anyone else can only cancel it.
- A room can't complete its own **approval checkpoint** ("Mike approves
  the prototype order") until a human-owed promise under the same goal
  has been answered — ask first with `delegate_goal`.
- Promise due times are rendered in local time with offset.

## The Work block

Every conversation's prompt carries one **Work** block, rendered fresh
each turn:

- **Goals this conversation holds** — outcomes with state and next wake
- **Owed to this conversation** — promises others owe it, marked
  "already recorded — do NOT add again"
- **Suggested** — dream offers awaiting an answer here
- **Asked of this conversation** — promises it owes others, with the exact
  `close_goal` call to settle them
- **Running in the background** — live background turns and subagents

## Rooms: how an outcome gets worked

Each outcome gets a **room** — a private utility conversation
(`agent:goal-<id>:utility`) that works exactly that goal. The **charter**
pasted into it is its behaviour spec (re-stamped from code at every
boot); its history is its working memory.

**The state block** (`room_state`) is the living worksheet: plan, known
facts (append-only evidence), open questions, next actions, the
strategies tree (hypotheses with verdicts — pruned branches stay), and
the artefact registry (`goal_artefact(path, what)`). Files go in
`goals/<id8>/` in the workspace.

**The loop.** Every room turn ends with a continuation declaration:
`goal_continue_now(reason)` (capped), `goal_wait(minutes|until, reason)`,
nothing when children are pending (their closes wake the room), or
`room_close(evidence)` / declaring blocked. A dead-man heartbeat means no
room goes silent by accident. Budget is counted in rounds; near the cap
the frames warn, at the cap the next round must close, declare blocked,
or ask the origin for renewal (owner-only). A room's first round opens
with **decompose first**: one child per person/item in a single
`add_goal(parent_goal_id=…, children=[…])` call, and an approval child
before anything paid or irreversible.

**Talking to humans.** A room's final text is delivered to **no one**.
- To tell the origin something (progress, results, an artefact):
  `send_report`.
- To ask a human (approval, a decision, a choice): `delegate_goal(to=<the
  origin conversation>, text=<the exact question with specifics>)`. Their
  answer wakes the room.
- Reporting the goal's own work to its origin needs no extra approval.

**What a room can use.** A room runs with its **creator's trust**
(`room_creator_principal`): a room for Mike can do what Mike's chat can;
a room for an untrusted member gets that member's narrower set. It gets
the chat toolset — memory (`recall`, `find`, `remember`,
`memory_correct`), history search, contacts lookup, background jobs
(`run_bg_process`, `bg_*`), subagents, docs, web search (MCP) — plus the
room tools (`room_state`, `room_close`, `room_spawn`,
`read_group_history`, loop tools), approvals, `send_report`, and DM
outreach. **Withheld**, because rooms run on timers with nobody watching:
`email_send`, `write_routine`, `delete_routine`, `create_contact` — ask
via `delegate_goal` or `request_approval` instead.

**Channel rules.**
- Per-person work (offers, chases, confirmations) happens in **that
  person's DM** — a promise whose completer is the DM. Never chase
  individuals through a group; room→group delegation is refused.
- Shared artefacts are **group content**: post them to the origin as they
  land.

## Who does what

| Work | Goes to |
|---|---|
| Quick answer, one-off script | The current turn (long turns move to the background automatically and keep every tool) |
| Needs Bob's memory, people or chat history | Bob himself, or an outcome goal with a child per person/item — **never a subagent** |
| Code, file processing, builds in the workspace | A **Claude subagent** (`create_subagent`, files + bash only; briefs that need memory/history are refused) |
| Minutes-long mechanical jobs (renders, crawls) | `run_bg_process` — wakes the conversation when done |
| Something owed back later | A promise (`add_goal(profile="promise")`) |
| Asking another chat or person | `delegate_goal` (chats) / `send_whatsapp_to_contact` with an `objective` (people). Without an `objective` it's a one-way delivery: nothing tracked, nobody woken |

## For operators

- **Dashboard → Work** (`/dashboard/work`): goal tree with promises,
  principal, loop status, next wake, live runs (linked to their turn
  trace), standalone promises, suggestions, background runs, settled
  history, and cancel. Goal detail: `/goals/<id>` (state, strategies,
  promises, wake/turn timeline).
- **API:** `GET /dashboard/api/work`, `GET /dashboard/api/goals[/<id>]`.
- **Kill switches / knobs:** `BOB_GOAL_ROOMS=off` (no rooms),
  `BOB_GOAL_LOOP=off` (fixed check-ins instead of the loop),
  `BOB_GOAL_LOOP_*` (budgets, caps, dead-man interval),
  `BOB_GOAL_ROOM_MAX_CHILDREN` / `_MAX_DEPTH`, `BOB_OUTREACH_VIA_TASKS`.
- **Scripts closing promises:** `BOB_TASK_TOKEN` authenticates the script
  completion endpoint.
- **Data model:** `docs/datamodel.md` → Goals.
