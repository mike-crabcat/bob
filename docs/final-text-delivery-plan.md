# Final-Text Delivery — inverting the send-tool contract

**Status:** agreed with Mike 2026-10-01, implementing.
**Supersedes:** the "text output is NOT delivered, you MUST call
send_whatsapp_message" contract (and the 2026-09-30 detach-note wording
fix, which never moved flash).

## Why

The old contract existed to encourage group silence: replying required a
deliberate tool call. Two things made it obsolete:

1. **Silence moved to better machinery.** The attention probe gates
   *waking* on unaddressed group chatter (engagement-first, STAND_DOWN
   default); claim-gating decides whether a turn runs at all. The
   send-call gate was the last remnant.
2. **The platform already inverted it in practice.** The send-tool rescue
   auto-delivers final text on human-stimulus turns ~13×/day (89 firings
   in the week before this doc) whenever the model skips the call — GLM's
   chronic send-skip. The prompt lied; the runner corrected it. On the
   flight/relay surface the correction doesn't reach, which cost a full
   relay turn + terse dead-man delivery per incident (25 dead-mans that
   same week).

## The contract

> **A turn's final text IS the reply — delivered automatically.** The
> send tool is for speaking *before* the turn ends: brief progress
updates while working, or an answer with media attached (media cannot be
backstopped). Silence is the explicit marker `NO_REPLY` as the entire
final output (decorated variants like `[NO_REPLY — reason]` still count).

## Scope — who auto-delivers

One predicate, no exceptions:

| turn class | auto-delivers final text? |
|---|---|
| DM / group conversation turns (`whatsapp_incoming`, human-stimulus) | **yes** |
| group member-change turns | **yes** (unchanged) |
| detached flights (inherited human stimulus) | **yes — at terminal** |
| routine turns (`call_category="routine"`) | **never** — explicit sends only |
| wake nudges (goal folds, memory) | **never** (`_SILENCE_OK_PROVENANCES`) |
| steer / steer_relay / reactions | **never** (decline = designed outcome) |
| email | unchanged (own explicit reply/skip contract) |

Gates that survive the flip, each paid for by an incident:
- `is_no_reply` — the silence primitive (2026-09-23 prose-swallow fix
  keeps whole-message matching for the prose variants).
- echo guard — never parrot the inbound stimulus back (2026-08-30).
- narration guard — claimed-but-not-made tool calls get stripped after
  one correction retry (2026-09-19).
- silence-ok provenances — un-sent text on steer/wake turns is the
  decline working, not a dropped reply (2026-09-07, 6×/morning leak).

**The gate that flips:** final-text delivery no longer requires
`not message_was_sent` — a progress update sent mid-turn must not eat
the answer that follows it. This is load-bearing: "progress send, then
final answer as text" is the new intended shape.

## Flights at terminal (relay retirement)

`_terminal` delivers silent-flight results directly through the send
handler (idempotency keys, send records, bg bookkeeping all preserved):

| flight ending | terminal action |
|---|---|
| spoke | nothing (its attributed messages were the output) |
| text, ran tools, not NO_REPLY | deliver `(background result)\n<text>` |
| text, **zero** tools, not NO_REPLY | deliver with unverified header — the `_narration_only_content` wording becomes a delivery prefix, not a relay instruction (2026-09-17 phantom-build: the harm was the system vouching; the header un-vouches) |
| failed | deliver `(bg turn failed…)` framing |
| NO_REPLY / empty | settle quietly |
| steer-born | silent on every terminal state (nobody asked) |

Send-handler failure at terminal falls back to the legacy wake (rare,
bridge-down case). `task_relay` wake creation ends for runtime
terminals; the dead-man switch stays as pure transition backstop, and
boot recovery (`recover_orphaned_goals` — no live spec to deliver with)
keeps the wake path.

## Sweep

- `dispatch_runner.py` — rescue block becomes the main delivery path;
  drop the `message_was_sent` condition; reword the narration-guard
  correction; dead-man untouched.
- `backburner.py` — terminal delivery table above; `delivery_note`
  rewritten to teach the new contract to flights;
  `_fallback_content`/`_narration_only_content` deleted (no callers);
  `_failed_content` kept for boot recovery.
- `prompt_assembler.py` — "## CRITICAL: How to Respond" block rewritten.
- `_service.py` — send tool description reframed (progress + media).
- `session_agenda_service.py` — WHATSAPP_* agendas reworded (email/phone
  untouched).
- Rollback: deliberately NO env kill switch — the contract spans prompt +
  runner + terminal, so a partial switch would leave a prompt that lies
  one way while the runner behaves the other; and an env flip needs the
  same restart a `git revert` does. Rollback = revert the commit +
  restart bob.service.

## Gates

- Unit tests: terminal framings, gate flips, routine/wake
  not-delivered, fallback-on-send-failure.
- Eval battery (both pairing models, pre/post): B2 rewritten (final text
  answers — send no longer required), B1 repurposed to a NO_REPLY
  silence pin, send-mock docstrings swept to the new production text,
  skill_delegation ack pin becomes ack-either-way. Guard integrity
  blocks deploy.
- Watch after deploy: duplicate sends (answer via tool + final text
  during transition), leaky silences ("nothing to add" prose), media
  answers with no tool call (unfixable by backstop — count them).
