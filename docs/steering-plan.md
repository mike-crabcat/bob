# Steering — requests that wake a target conversation

Status: **implemented 2026-08-30** (same day as the design discussion; tests
`tests/test_steering.py`, 17 passing) · `services/steering.py` + bridge
attach + `prompt_assembler` steer provenance + migration `464_steering.sql` ·
`group_send_approval.py` and `request_group_message` deleted, `tests/test_group_send_approval.py`
replaced. Replaces the verbatim cross-group relay (`request_group_message`)
shipped 2026-08-28.

## Goal

Today a user can ask Bob to *say an exact thing* in another conversation:
`request_group_message` captures requester-dictated text, the owner approves
per message, and the platform delivers it verbatim
(`services/group_send_approval.py`). Instead, a user should be able to
**steer** a target conversation: express an intent, and the target
conversation gets a fresh wakeup turn that composes its own message in its
own context.

> "Let AI Doom group know that this radio feature set is on"
> → steering wake in the AI Doom conversation
> → that turn pulls the radio promo material from the workspace and posts it
>   in Bob's voice for that group, aware of what's already been said there.

The requester dictates *intent*; the target turn dictates *text*. Steering
reuses the proven wake primitive end-to-end: `wake_conversation`
(`services/wake_service.py:67`) already stores content as an undispatched
user message and dispatches a full hardened inbound turn (attention
coordinator, turn claims, effects-backed sends).

## Decisions (settled with Mike 2026-08-30)

1. **Non-owner steers go through the approval system** — the owner approves
   the *intent*, not a message. Owner self-steer bypasses, matching the
   existing owner-bypass pattern in `group_send_approval.create_request`.
2. **The steering instruction never reads as human speech in the target.**
   It renders as a labelled steering request from a named requester via
   another conversation — the provenance-labelling machinery in
   `prompt_assembler.py:415-426` (wake_nudge / group_event today) grows a
   `steer` provenance.
3. **No old group-send gates or limits apply.** The steering turn's reply
   rides the target's normal reply path (`send_whatsapp_message`), which has
   no `group_outbound_enabled` check and no `_group_send_allowed` limiter —
   and we are not adding one at trigger time.
4. **Replace, don't add.** `request_group_message` retires, along with the
   `group_send` approval type and its delivery path. The proactive
   policy-gated `send_whatsapp_group_message` stays — it is a different
   feature (goal-driven proactive sends), out of scope here.
5. **Anyone can steer, subject to approval.** No trust restriction on who
   may *request* a steer (owner direct, everyone else approved). Membership
   still binds the *target*: a user may steer only their own DM or a group
   they participate in — this applies to the owner too; relaxing it later is
   a one-line change if operator-wide steering is ever wanted.

## Non-goals

- **No completion receipt back to the requester (v1).** The requester is by
  definition a participant of the target, so they see the outcome there.
  The gap — silence when the target turn decides not to send — can be a
  follow-up wake-back if it bites.
- **No steering from wake-path turns.** The tool attaches only when a human
  contact dispatched the turn (contact_id present); a steering wake in a
  group carries no contact, so no nested steering.
- **WhatsApp-only v1.** DMs and groups. `wake_conversation`'s generic
  non-WA dispatch path exists, but participant/membership semantics are
  WhatsApp's; email-channel steering can come later if wanted.
- **No new rate limiting** (decision 3).

## Mechanics

### The tool

`steer_conversation(target, instruction)` — one tool, attached in the
bridge's per-dispatch tool build (`whatsapp_bridge_service/_service.py`,
around the `make_group_send_tools` site at :864) **on any dispatch a human
contact started** (`human_initiated` on `_build_inbound_dispatch_spec`), DM
or group, any trust level. Wake-path turns never steer, even when the route
resolves a contact id — the gate is dispatch origin, not contact resolution.

The tool description does the UX work — users never say "steering". It must
trigger on natural phrasings ("tell the Leeming Boys chat about this
verdict", "let AI Doom know the radio feature is on", "give the events group
an update"), and it must instruct the calling LLM to make `instruction`
**self-contained**: resolve references local to the origin conversation
("this verdict" → a one-line summary or a workspace pointer) at call time,
because the target turn wakes with no context of where the request came
from. The description should also say what comes back — steered-now vs
pending-owner-approval — so the requesting turn can relay that to the user.

Target resolution — the membership gate falls out of resolution, because
candidates only come from the requester's own conversations:

- Own-DM references ("my chat", "our dm") → the requester's DM session key
  (`agent:main:whatsapp:dm:{digits}`), requiring an active binding.
- Group name or id → fuzzy-matched (find_session-style, defensive — the LLM
  invents identifiers) **against `groups_for_contact(contact_id)` only**
  (`repositories/groups.py:162`). Ambiguous → return the candidate list and
  let the requesting turn ask. Not a participant / no such group →
  fail closed with that reason.
- Target == current conversation is allowed and degenerates to a nudge; no
  special case.

Tool result shapes mirror `create_request`'s: `{"ok": true, "steered": true}`
for owner-direct, `{"ok": true, "approval_id": …}` when pending, so the
requesting turn can tell the user "Mike needs to approve that".

### The approval flow (non-owners)

New approval type `conversation_steer`, reusing the shape of
`group_send_approval.py` (whose `owner_contact` / `owner_dm_session_key`
helpers move into the new module):

- Proposal carries: target key + label, requester contact id + name,
  **the instruction verbatim**, origin session key. The proposal summary is
  what the owner sees in their DM wake — target, requester, instruction.
- Dedupe: an identical pending (target, instruction) returns the existing
  approval instead of minting a second.
- `on_approved` hook fires the wake; the wake effect's idempotency key is
  `steer_wake_approved:{approval_id}`, so a redelivered `approval_respond`
  can never double-wake (same property `send_idempotency_key` gives today).

### The wake

A new effect kind `conversation_steer`, registered via
`effects.register_executor` at startup (like `whatsapp_send`,
`approval_request`). The executor calls `wake_conversation`; making the wake
effect-backed buys durability, ops visibility in the effects outbox, and the
idempotency keys above. Not retryable — if the bridge is down the message is
stored undispatched and the startup sweep / next inbound dispatches it, which
is already wake_conversation's crash semantics.

Wake content stored in the target conversation:

- provenance **`steer`** (new, alongside `wake_nudge` / `group_event`) so
  `prompt_assembler` labels it and it never reads as a participant speaking;
- rendered with a header like `[Steering request — {requester name}, via
  {origin conversation label}]` followed by the instruction verbatim, plus
  guidance that this is a request from a named person to act on, not an
  operator directive;
- metadata: `requester_contact_id`, `requester_name`,
  `origin_session_key`, `approval_id?` — group bindings carry no contact, so
  metadata is how the target turn knows who asked.

The target turn composes and replies via its normal send path. Nothing about
its toolset changes.

### Retirement

Delete `request_group_message` from `whatsapp_outreach_tools.py` and the
`group_send_approval.py` module (its `register()` binding, `on_approved`,
`send_idempotency_key`; keep nothing dormant). Before cutover, check for
pending `group_send` approvals — expected zero, the feature likely never
saw a real non-owner request; any found are expired with a note to the owner
rather than silently dropped. Migration 462 stays (history).

## Implementation steps

1. `services/steering.py` — tool factory + target resolution + approval
   request creation + `on_approved` + the `conversation_steer` effect
   executor + `register()` binding both hooks; move the owner-lookup
   helpers here.
2. Bridge tool build — attach `steer_conversation` when contact_id is
   present; drop `make_group_send_tools`' `request_group_message` (keep
   `send_whatsapp_group_message`, still trusted-only + policy-gated).
3. `prompt_assembler.py` — `steer` provenance labelling.
4. Registration — no `main.py` change: `approval_tools`' registration tail
   (the same site that bound the old group_send hook, chosen so the pump can
   never deliver an approval_respond with the hook missing) imports steering
   and binds both the executor and the on-approved hook; steering.py also
   self-registers at import.
5. Migration `464_steering.sql` (the plan's "no migration" guess was wrong —
   `approvals.approval_type` carries a CHECK constraint): rebuilds the table
   with `conversation_steer` in the enum, and cancels any still-pending
   `group_send` approvals with an explanatory review note — their delivery
   hook is gone, so leaving them pending would wedge the owner's list.

## Testing

Mirror `tests/services/test_backburner.py`-style coverage:

- **Resolution**: own-DM happy path; group by name and by id; ambiguous name
  returns candidates; non-participant and unknown targets fail closed with
  usable errors; invented ids never resolve.
- **Approval**: owner bypass wakes directly; non-owner routes to the owner's
  DM with target + requester + instruction in the summary; duplicate request
  dedupes; redelivered approve cannot double-wake (idempotency key).
- **Wake rendering**: stored message carries provenance `steer` + requester
  metadata; prompt assembly labels it as a steering request, not speech.
- **Retirement**: `request_group_message` gone from the tool surface;
  `send_whatsapp_group_message` untouched.

## Rollout / watchpoints

- Careful-rollouts lesson: owner self-steer works day one with zero risk —
  Mike should run the first real steer himself (e.g. the radio promo to AI
  Doom) before any non-owner request arrives.
- **First real steer (2026-08-30 13:25, radio promo + poster to Leeming
  Boys): worked end-to-end** — wake landed labelled, target turn composed
  and attached the poster in 31s. But the requesting turn burned ~7
  exploratory calls (docs_search ×2 → empty, then bash-grepping the server's
  own source) before steering, because the tool descriptions didn't say
  whether steering can carry an image. Fixed same day: steer + send tool
  docstrings now state the media story and disambiguate each other, and
  `workspace/docs/messaging.md` gives docs_search an answer for "send image
  to group" queries. Watch the next few steers for tool-selection cost.
- **Tool-selection drift → structural split (2026-08-31):** docstring
  disambiguation did not hold. One "advertise this set in ai doom and
  Leeming" request in the radio group used both tools — a direct
  `send_whatsapp_group_message` to AI doom (the only group with the policy
  flag on, so it *succeeded* — twice, 27s apart), a steer for Leeming whose
  instruction dictated the advert "verbatim", and the steered turn itself
  fanning out six more direct sends before using its own reply path. Both
  tools attached on trusted human turns, so the model could pick wrong.
  Fix: the attach is structural now — `_build_inbound_dispatch_spec` takes
  `human_initiated` (live inbound passes True; `wake_session` derives it via
  `has_undispatched_inbound`, so post-call occupancy drain and crash
  recovery of human messages still count as human). Human turns get steer
  only; autonomous turns (goal deadlines, reviser wakes, nudges — Bob
  Events' proactive path) get the group-send tool only. Regression tests:
  `tests/services/test_steer_groupsend_split.py`.
- **Prompt-injection surface**: a steering instruction is requester-authored
  text entering a privileged turn with workspace tools. The labelling in (2)
  is the control — it must render as "X asks…" context, never as an
  operator instruction. Watch the first few non-owner steers for target
  turns over-complying with odd asks.
- **Backburner interplay**: no special-casing — a steering wake is classified
  by whatever the target conversation already does (groups stay inline;
  DMs follow the probe's existing nudge/wake-path treatment). Verify in
  tests that the probe doesn't misfire on `steer` provenance.
- **Dashboard**: steering messages appear in conversation history with their
  provenance; rendering them as a distinct row kind in the conversations
  view is a nice-to-have follow-up (render-distinct-entity-types lesson),
  not launch-blocking.
