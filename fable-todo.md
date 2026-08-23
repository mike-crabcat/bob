# fable-todo.md

Deferred work identified during the Bob 3 wrap-up (2026-08-23). Owned by
nobody yet — pick up when prioritised.

## 1. Audience-based memory visibility (M) — the Phase VI item 3 residual

Memory visibility today is place-based at write time (claims tagged
`group-<jid>` / `<contact_id>` derived by parsing session keys in
`services/memory/channels.py`) and **largely unenforced at read time**: the
memory index injected into prompts lists all entities globally and
`get_active_claims` has no visibility clause. Privacy in practice rests on
prompt guidance + LLM judgment. Participant churn doesn't break anything
mechanically — but only because nothing is person-scoped to begin with; the
real failure mode is a DM-learned fact surfacing in a group.

Plan (per bob3-plan.md Phase VI item 3 sub-goal, "memory visibility
recalculated from participants + item audience, not parsed keys"):

- Schema: `audience_json` on claims — the set of person ids present when the
  claim formed (group participants at write time; DM = {contact}).
- Write path: capture audience from `session_participants` at claim creation.
- Read path: filter memory index, search tools, and claim injection by
  `audience ⊇ current conversation participants` (a claim is visible only if
  everyone present was in its original audience, or it is explicitly public).
- Backfill: existing claims get audience from their scope tag (group scope →
  current group membership snapshot, documented as approximate).
- Exit: the plan's "audience test matrix" — new-member-joins doesn't unlock
  DM-learned claims; member-leaves changes nothing; explicit public claims
  visible everywhere.

## 2. Reconciliation timestamp-format false positive (S)

`session_messages.created_at` mixes `YYYY-MM-DD HH:MM:SS` and ISO-`T`
formats; the heartbeat `event_log_reconciliation` TEXT-compares against a
T-format bound so it counts only T-format rows (reported legacy=4 vs actual
~210 on 2026-08-23). Normalise both sides (e.g. `replace(created_at,' ','T')`)
or compare on `datetime()`; then re-check whether a real gap exists
(~210 messages vs 195 events in the window — likely wake-synthetic user
messages, verify and either exclude them or reconcile).

## 3. Undelivered tap output recorded in merged history (S)

When the first LLM response is empty, the tap second-chance can return text
that was never sent (no send-tool call, no effect) — with
`history_policy="merged"` it lands in assistant history anyway (observed
2026-08-23 01:01: byte-identical repeat of the previous reply stored,
nothing delivered). Options: skip recording tap output when
`not message_was_sent[0]`, or switch groups to `delivered_only`.

## 4. subagent_service full dissolution (M) — Phase VI item 5 residual

Spawns are now durable `subagent_spawn` effects (executor kinds claude/local)
and voice is a binding, but `subagent_service.py` still owns run/message/
check/list/kill lifecycle. The plan's end state dissolves it into the goal +
executor machinery. Non-urgent; the alias-normalisation mess is already
contained.

## 5. Conversation-centric rework of remaining dashboard pages (S–M)

Dashboard v3 increments 1–4 are shipped (health strip, conversations rename +
decision timeline, goals & wakeups page, merge badges/provenance/unmerge).
Deliberately deferred from increment 4: reworking contacts, phone, and skills
pages to link into `/conversations/*` natively (they still link/label by
legacy session concepts where applicable). Do this opportunistically when
touching those pages.
