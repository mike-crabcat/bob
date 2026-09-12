# Detach v2 — attributed background turns (replaces capture-and-relay)

**Status:** IMPLEMENTED + DEPLOYED 2026-09-12 (commit 3b809c64, suite 887
green; plan written same day from Mike's design). Supersedes the
capture/relay mechanics of `docs/backburner-plan.md` (mode ladder, probe,
subagent/goal registration, supervisors all stay; only the delivery contract
changed). Rollback: `backburner.mode = hold`.

---

## How detach works in the finished state

A detached turn is a **labelled participant in its channel**, not a silent
worker that reports through a proxy:

1. **Detaching changes nothing about delivery.** A turn that detaches keeps
   its tools and keeps acting on them — bash runs, sends deliver, steers go
   out. There is no capture mode and no suppression. The only thing that
   changes is *attribution*.
2. **The transcript gets a placeholder at detach.** The moment a turn
   detaches, its conversation's history gains one synthetic row:
   `[bg task 4545bcaa detached: "share coffee gif over active groups".
   Its messages will appear under that id until it finishes.]` Every later
   LLM turn in that conversation sees it, so background work is never
   invisible or mistaken for the live turn's own voice.
3. **The flight's channel messages are attributed.** Anything the detached
   turn sends after detaching is delivered immediately and recorded in
   history with the subagent id as sender. Later turns render it as
   `[bg 4545bcaa] <text>` in their transcripts. The same attribution rides
   on steers and group-sends the flight makes, so target conversations see
   the background origin too.
4. **Completion is a marker, not an instruction.** When the task finishes
   and it already spoke, nothing further happens — its messages were the
   output. A flight that finishes **without ever sending** gets one minimal
   attributed completion notice carrying the result (no "deliver this"
   imperative anywhere). Silence after speaking is also fine: no relay turn
   is spawned to re-deliver, summarize, or interpret.
5. **The holding ack still goes out at detach** ("on it — I'll report
   back"), and the late-arriving attributed messages read as exactly that
   promised report.

**Worked example** (the 2026-09-11 coffee request, under v2):

```
09:59:23  Mike: share the coffee gif over active groups
09:59:25  [turn starts; gif hunt is slow…]
09:59:59  [bg task 4545bcaa detached: "share coffee gif…"]   ← placeholder
09:59:59  Bob (live): on it — grabbing the right one         ← holding ack
10:00:15  five groups steered (attributed to 4545bcaa in their transcripts)
10:00:44  [bg 4545bcaa] ☕ + clip, in each of the five groups  ← direct, real
10:00:50  task completes → no relay turn, no re-delivery      ← done
```

One gif per group. No second turn is ever told "nothing was delivered".

---

## Why: capture's invariant was narrower than its relay claimed

Capture mode mutes exactly one tool (`send_whatsapp_message`). Every other
side-effecting tool — bash (cryptobro sells), `steer_conversation`,
`send_whatsapp_group_message` — executes for real during a flight. But the
relay template (`_relay_content`, backburner.py:461) asserts *"nothing in it
has been delivered to anyone… waiting for you to deliver it"* — a blanket
falsehood whenever a non-captured tool ran, and an instruction to act on it.
Three incidents, one mechanism:

- **2026-08-30 era (messages):** dual-post through steer + direct send —
  patched per-turn by the tool split, but the seam stayed.
- **2026-09-10 12:23 (crypto):** detached flight sold via bash (PUMP filled),
  relay said nothing was delivered, the relay turn re-ran the sells 13s
  later — the "two concurrent tasks" that overwrote the first ENA ledger row.
- **2026-09-11 10:00 (coffee gif):** detached flight steered five groups
  (real, uncaptured), relay instructed a fresh turn to deliver, fresh turn
  direct-sent the correct clip — two gifs per group.

The 2026-09-05 "delivery-truth template" fixed relays that claimed sends
which never happened, by over-claiming the opposite. v2 removes the claim
instead: sends are real, so there is nothing to lie about.

## Design decisions

| decision | why |
|---|---|
| attribution over muting | the relay's re-delivery instruction was the incident mechanism; labelled participation removes the class, not the instance |
| bash stays real | it is the work; the placeholder makes later turns aware a flight owns it (they check state, not redo) |
| placeholder row over goals-only visibility | goals_block half-carried in-flight work; a transcript row is what turns actually read, and it survives goal settling |
| keep the tee (`sent_texts`) | the dead-man rescue and the audit trail need the record; only suppression dies |
| keep the probe + holding ack + subagent/goal registration + supervisors | unchanged machinery; only steps (e) capture, (f)'s conditions, and `_relay_content`'s regime change |
| silent-flight fallback notice, nothing more | computed-but-unsent results must not vanish (2026-09-03 Andrew's video) — but the notice carries no imperative |
| no dual-mode flag | rollback is the existing `backburner.mode` ladder (off/hold disable detaching); carrying both delivery semantics doubles the surface we're deleting |

## Parts

### Part 1 — send wrappers: suppression → tee + attribution

`whatsapp_bridge_service/_service.py` and `_group_events.py`,
`_send_whatsapp_message`:

- Delete the `backburner_capture` suppression branch (the "captured, will be
  relayed" return).
- Keep appending to `sent_texts` (the tee) — now on every post-detach send.
- Post-detach sends write history with `sender_id = "bg:<subagent8>"` and
  metadata `{"background_task": subagent_id}`; tool response becomes
  `delivered (attributed to task <id>)` so the model learns the regime from
  tool results mid-flight, as it does today.
- The detach race guard (`llm_task.done() or message_was_sent` → wait
  inline) and the leaked-markup guard stay.

### Part 2 — the detach placeholder

In `detach()` after registration: one `SessionService.add_message(
session_key, "system"|"assistant", placeholder_text, metadata=
{"background_task": id, "placeholder": True})`. Content carries the task
short id and the probe summary. Prompt rendering must include it (see
Part 3) and mark it non-speakable (never quoted, never re-delivered).

### Part 3 — transcript rendering

`prompt_assembler.py` (group sender-prefix machinery at ~:481/:572):

- Render assistant rows whose metadata names a background task as
  `[bg <id8>] text`; render the placeholder row as its literal content.
- DM sessions have no sender prefix today — add the `[bg …]` prefix
  conditionally for these rows only.
- Human-visible WhatsApp messages still arrive as Bob (same number);
  optionally prefix the wire text with nothing — the holding ack already
  set the async expectation. (Human-visible tagging = open question 3.)

### Part 4 — terminal: marker + silent-flight fallback

`_terminal` / `_relay_content`:

- Spoke-then-finished: settle the goal quietly; **no wake, no relay turn**.
- Finished-without-sending: one attributed wake carrying the result with
  honest wording — "task <id> finished without posting; result attached for
  context" — and no delivery instruction.
- The failed/deadline paths keep their wake (something must be said), with
  the same attributed, non-imperative wording.
- `steer_relay` label and quiet-path semantics fold into this: steered
  flights already treat silence as intent; now they may also speak directly
  and attributed instead of relaying.

### Part 5 — attribution on cross-conversation effects

When the flight uses `steer_conversation` or `send_whatsapp_group_message`,
the resulting rows in the TARGET conversation carry the same background-task
metadata, so those transcripts also render `[bg <id8>]`. This is what fixes
the coffee shape at the target side, not just the origin side.

### Part 6 — cleanup

- Delete `backburner_capture` plumbing (spec field, enabling at detach step
  (e), both send-wrapper branches, holding-ack `_send_holding_ack`'s
  capture-related conditions if unneeded).
- `_relay_content` shrinks to the fallback wording above; the
  "delivery truth" preamble is deleted with the mechanism it described.
- Docs: backburner-plan.md gets a v2 pointer; memory updates on merge.

## Tests

- `tests/services/test_backburner.py`, `test_relay_guards.py`: the
  capture/relay semantics flip wholesale — post-detach sends deliver;
  no relay wake on spoke-then-finished; fallback notice on silent finish;
  placeholder row written at detach; tee still records for rescue.
- New: transcript rendering (`[bg …]` prefix in DM and group prompts),
  attribution metadata on steer/group-send rows from flights, rescue path
  over the tee, race guard unchanged.
- Live-fire rehearsal before rollout: re-run the coffee scenario
  (multi-group media fan-out from a DM instruction) and a slow bash turn;
  expect one gif per group, attributed transcript rows, no relay turns.

## Rollout

1. Land behind nothing (no dual mode), but deploy at a quiet hour; the
   rollback is `backburner.mode = hold` (holding ack, no detach) via
   config — same lever as today.
2. Watch for 48h: duplicate sends (should be structurally zero), detached
   flights that finish silent (fallback frequency — if high, prompt the
   flight at detach to speak when done), transcript complaints from later
   turns misreading `[bg …]` rows.

## Risks (accepted or mitigated)

1. **Stale speech, unfiltered** — flight content is computed against frozen
   context; attribution labels the speaker, not the staleness. Accepted:
   the holding ack frames lateness; watch for human confusion.
2. **Unserialized channel writers** — live turn and flight can interleave;
   near-duplicates possible ("on it" vs the flight's answer). Accepted;
   watch during rollout.
3. **Weak regime signal** — "delivered" may teach flights that sending is
   conversation. Mitigated: the post-detach tool response must read
   "delivered (attributed to task <id>) — you are a background task;
   continue your work". Watch for flights stopping early after first send.
4. **Labels as silent-failure dependency** — if the placeholder or [bg]
   metadata drops out of any prompt path (folding's 20-message window is
   the main hazard), later turns misattribute silently. Mitigated: PIN the
   placeholder row against the history trim; render-labels is a tested
   requirement, not a nicety.
5. **Fallback is a relay-in-waiting** — the silent-flight notice must stay
   non-imperative by TEST (wording asserts no "deliver" instruction), not
   by convention.
6. **No middle-gear rollback** — a bad deploy degrades to mode=hold (no
   detaching). Accepted; deploy at a quiet hour.

## Non-goals

- Capturing/deferring bash — it is the work.
- Fixing the `human_initiated` derivation on relayed wakes — v2 removes the
  relay turn that misclassification armed (the coffee relay turn). The
  classification seam itself stays as-is unless it resurfaces.
- Human-visible sender identity on the wire (WhatsApp shows Bob either way).

## Open questions

1. **Multi-fragment etiquette** — should post-detach sends be bounded (e.g.
   the model gets nudged to consolidate after N fragments), or is labelling
   enough?
2. **Placeholder durability** — keep the placeholder row after the task
   finishes (permanent transcript context) or collapse it to a one-line
   completion marker? Lean: keep, cheap and auditable.
3. **Human-visible tag** — prefix wire text with a subtle marker (e.g. a
   trailing `— bg`) or trust the holding-ack framing? Lean: trust the
   framing; revisit if humans report confusion about late messages.
4. **Interaction with the tool-loop-folding history view** — `[bg]` rows
   must survive the 20-message window trim; may need pinning like the
   placeholder.
