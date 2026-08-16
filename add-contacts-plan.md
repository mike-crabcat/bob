# Add-contacts — outbound contact creation for the agent

Status: draft plan · Branch: `add-contacts` (suggested) · Written 2026-08-16
Companion press release: `add-contacts-press-release.md`

## Goal

Let Bob create contacts himself so he can place calls (and any future outbound
contact) to numbers that aren't already in his contact list — closing the dead
end hit on 2026-08-16 when a "call JB Hi-Fi Osborne Park" request was blocked
because the shop had no contact row and the agent had no tool to add one.

The inbound trust boundary moves with it: agent-created contacts exist in the
contact list but **cannot DM Bob**. "Exists in the directory" and "may open a
DM session" become separate facts.

## Non-goals

- **Raw-number call placement** (`create_subagent(openai_voice, phone_number=…)`
  without a contact). `contact_id` anchors session attachment, memory, and
  report-back routing; a contactless call path needs fallbacks everywhere. The
  create tool gets the same outcome for a fraction of the change.
- **Trust escalation.** Tool-created contacts are `is_trusted=0`, same as
  `/approve` today. Trust stays a dashboard-only decision.
- **Email-only contacts from the agent.** The motivating case is phone; the tool
  takes name + phone. Email addresses can be added via dashboard/REST as today.
- **Letting agent-created contacts open the inbound DM gate.** Explicitly the
  opposite: that's the `allow_inbound_dm` split below.

## Settled design decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | New column `contacts.allow_inbound_dm INTEGER NOT NULL DEFAULT 1`; the inbound gate checks it instead of bare contact existence | Existence is overloaded today (`_service.py:792`): it simultaneously means "directory entry" and "may DM". Default 1 keeps every existing human, group auto-seed, and dashboard/REST create on today's behaviour — the migration is behaviorally inert until the new tool writes a 0. |
| D2 | Agent tool `create_contact(name, phone_number)`, offered to **trusted sessions only** | Contact rows are load-bearing (inbound gate, session routing, memory links). Untrusted senders must not be able to mint entries in Bob's directory. |
| D3 | Tool-created contacts get `is_trusted=0, allow_inbound_dm=0` | Outbound-only by construction. A shop in the list is harmless; the same shop's number opening a WhatsApp session is a different trust question nobody asked. |
| D4 | Dedupe by phone **never mutates an existing contact's flags or real name** | A real person must not lose DM rights (or their name) because the agent "added" them for a one-off call. Existing contact → return its `contact_id` as-is. |
| D5 | `/approve` is removed, succeeded by the tool + dashboard toggle | `/approve`'s two jobs split cleanly: creating a contact from a phone number (now the agent's job, conversational) and pre-authorizing inbound DMs (now an explicit dashboard decision — flipping `allow_inbound_dm`). Consequence, accepted: there is no WhatsApp-side way to authorize a new inbound DMer anymore. |
| D6 | One shared phone normalizer, replacing the two divergent copies | `routers/contacts.py:21` (`_normalize_phone_number`) and `whatsapp_bridge_service/_media.py:24` (`_jid_to_phone`) differ subtly. Three callers after this change is the excuse to unify: new `services/phone_utils.py` with +61-defaulting normalization (leading-0, bare-61, `+CC`, >8-digit international, JID-safe); all callers import it. |
| D7 | Gate drop for flag-0 contacts logs a **distinct** line from unknown-number drops | "Contact exists but inbound DMs disabled" vs "dropped unknown whatsapp DM". Future "why can't X reach me" debugging depends on telling these apart in `journalctl`. |
| D8 | Tool still runs `ensure_person_entry` | Keeps the memory person-entity link in step with the directory, as `/approve` did (`_slash_commands.py:163-168`). |
| D9 | Outbound-only contacts are **visually distinct**, keyed on `allow_inbound_dm = 0` — not on who created the contact | The flag is the semantic that matters ("cannot open a DM session"), and it stays truthful when provenance doesn't: an operator-flagged contact shows the same marker, and an agent-created one loses it the moment inbound access is granted. Keying on creator would need a provenance column and would go stale the first time a toggle flips. |

## Data model

Migration `schemas/357_contacts_allow_inbound_dm.sql` (next free number —
`TODO.md` tentatively reserves 357 for the legacy-table drop; whichever lands
first takes it, the other renumbers).

```sql
ALTER TABLE contacts ADD COLUMN allow_inbound_dm INTEGER NOT NULL DEFAULT 1;
```

No backfill. Default 1 preserves all existing rows, the group auto-seed path
(`_service.py:807`), and REST/dashboard creates.

## The tool

`services/contact_tools.py` — `make_contact_tools(ctx, is_trusted=is_trusted)`:

- `search_contacts` — unchanged, still offered in every session that gets
  contact tools today.
- `create_contact(name: str, phone_number: str) -> str` — trusted sessions only.

Contract:

1. Normalize phone via the shared normalizer (D6). Unparseable → error string
   naming the accepted formats; never a silent guess.
2. Look up by normalized phone. Existing contact → return
   `{"contact_id": …, "created": false, "name": …, "phone_number": …,
   "allow_inbound_dm": <current>}`. No mutations (D4).
3. Insert with `is_trusted=0, allow_inbound_dm=0`, then `ensure_person_entry`.
4. Return
   `{"contact_id": …, "created": true, "name": …, "phone_number": …,
   "allow_inbound_dm": false}` — the id is what the LLM chains straight into
   `create_subagent(agent_type="openai_voice", modality="phone", …)`, and the
   `allow_inbound_dm: false` field is what stops Bob from later claiming the
   shop can WhatsApp him.

Registry wiring (`services/tool_registry.py:74-81`): both call sites pass
`is_trusted` through. Note line 81 extends contact tools to **untrusted**
sessions when the phone subsystem is enabled — the tool must be conditionally
appended, not assumed trusted because the module was reached.

## Inbound gate change

`whatsapp_bridge_service/_service.py:775-805`:

- Add `allow_inbound_dm` to the contact SELECT.
- In the `if contact:` branch, when `chat_kind == "dm"` and the flag is 0:
  log `"dropped DM: contact exists but inbound disabled: phone=%s contact=%s"`
  and return — same treatment as unknown numbers, distinguishable log (D7).
- Unknown numbers: unchanged drop+warn.
- Group chats: unchanged (group sync auto-seed keeps default 1).

## /approve removal

`whatsapp_bridge_service/_slash_commands.py`:

- Delete the dispatch branch (lines 39-40) and `_cmd_approve` (118-171).
- Sweep the slash-command help/usage surface for `/approve` mentions.
- No tests reference it; the CHANGELOG mention is history and stays.

## Dashboard / REST surface (the inbound-auth successor)

- `models.py`: `allow_inbound_dm` on `ContactCreate` / `ContactResponse`.
- `routers/contacts.py` `POST /api/v1/contacts`: accept the field (default 1).
- `dashboard_api/contacts.py:121` `update_contact`: accept the field.
- Contacts UI: an "Allow inbound DM" toggle alongside "trusted" — this is now
  the only path that grants a new number the right to open a DM session.

### Visual distinction for outbound-only contacts (D9)

The trust dimension already has an idiom: a `w-1.5 h-1.5` dot (`bg-success` vs
`bg-muted`) in both `ui_app/src/routes/contacts/index.tsx:77` and
`$contactId.tsx:249-250`, plus an all/trusted filter row. The new dimension
gets its own, quieter marker so the two never overload one glyph:

- **List** (`index.tsx`): a small muted chip next to the name — `outbound only`
  — when `allow_inbound_dm` is false. Muted styling: informational, not an
  alarm. Extend the filter row from `["all", "trusted"]` (line 52) to include
  an `outbound` view.
- **Detail** (`$contactId.tsx`): append `· outbound only` after the
  trusted/untrusted label at line 250, and place the "Allow inbound DM"
  toggle in the same edit form as `editTrusted` (lines 148-157).
- Both surfaces read the flag from `ContactResponse` — no extra endpoint.
- The marker self-clears when inbound access is granted; no provenance
  tracking anywhere.

## Prompt touch

One line in the voice-outreach/subagent tool descriptions
(`services/voice_outreach_tools.py`, `services/subagent_tools.py:34`):
unknown number → `create_contact` → call. So Bob never repeats the
"I can't add contacts with the tools I've been given" dead end, and knows
these contacts are outbound-only.

## Implementation phases

1. `phone_utils.py` normalizer + port the two existing callers (no behaviour
   change intended; covered by tests before anything else moves).
2. Migration 357 + models/REST/dashboard field (flag live but nothing reads it
   on the hot path yet).
3. Gate change (reads the flag; existing rows unaffected — default 1).
4. `create_contact` tool + registry threading + prompt touch.
5. `/approve` removal + help sweep.
6. Dashboard toggle UI.

Phases 1-3 are inert; 4 is the first agent-visible change; 5 removes the old
path only after the new one works.

## Testing & verification

- Normalizer: table test — `(08) 9244 5300`, `0892445300`, `+61892445300`,
  `61892445300`, `61456224867@s.whatsapp.com`, `+61456224867:12`, bare
  international `442079460000`, garbage.
- Tool: create / dedupe-returns-existing-id-without-flag-flip / trusted-gating
  (not offered untrusted) / unparseable-phone error / person entry created.
- Gate: flag-0 contact dropped with the new log line, flag-1 dispatches,
  unknown dropped, group message from a flag-0 contact still passes.
- UI: `outbound only` chip renders for flag-0 contacts in list and detail,
  disappears after the toggle grants inbound, filter view works.
- Rollout check (careful-rollouts practice): after deploy, confirm one real
  inbound DM from a known human still dispatches, and exercise the JB Hi-Fi
  flow end-to-end — `/call`-style request in chat → tool → Twilio dial.

## Open questions (decide during implementation)

- If a flag-0 contact's number later messages Bob's WhatsApp repeatedly, do we
  want a "requested inbound access" notification to the operator, or are the
  drop logs sufficient?
- When `create_contact` dedupes against an existing flag-0 contact and the LLM
  is mid-task, is the current no-mutation return informative enough, or should
  it hint the contact already exists for outbound use?
