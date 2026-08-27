# Press release — Bob learns to dial strangers

For internal release · Companion to `add-contacts-plan.md` · Written 2026-08-16

## Bob can now call any business, not just people he already knows

**Perth, Western Australia** — Bob, the AI assistant that runs Mike Cleaver's
messages, memory, and phone line, can now add contacts to his own address book
on request. Until now, asking Bob to phone a shop, a restaurant, or any number
not already saved as a contact produced a polite refusal: the calling system
required a saved contact, and no tool existed to save one.

The gap surfaced during a routine errand. Asked to call JB Hi-Fi Osborne Park
and check for a Sega Mega Drive 2, Bob replied that he was "blocked before
dialing" — the call tool only accepts saved contacts. Told to just add the
shop, he was forced to explain that he could search contacts but not create
them, and that he would not go "spelunking into the database with a crowbar."

That crowbar has now been retired. Say "add JB Hi-Fi Osborne Park, (08) 9244
5300, and call them" and Bob creates the contact and dials in one motion,
chaining straight into his voice-calling subagent with the number verified and
the task briefed.

### Calls out, but nobody calls in

The new ability comes with a boundary the operator chose deliberately: contacts
Bob creates exist in his contact list for outbound use — calls, lookups,
memory — but **cannot open a WhatsApp conversation with him**. Being in Bob's
directory and being allowed to message him are now separate facts, decided
separately.

A shop Bob phones about stock cannot later use that number to reach his
WhatsApp sessions. If a number should genuinely be able to message Bob, the
operator flips one toggle in the contacts dashboard — an explicit decision,
not a side effect of a phone call.

The WhatsApp-side `/approve` command is retired with this change. Its two
jobs are split between the person and the assistant: Bob handles the
directory, the operator handles the door.

### What it means in practice

- "Call the pharmacy on Murray Street and ask if my prescription is ready" now
  works without preparation.
- The contact list becomes a shared workspace: the agent adds what a task
  needs, the operator curates who counts as reachable.
- Outbound-only contacts carry a quiet "outbound only" mark in the dashboard,
  so a glance shows which entries can talk back and which can only be called.
- Inbound trust is unchanged: unknown numbers were already dropped before
  reaching Bob, and they still are — including numbers the agent itself added.

### Quotes

> "Just add jb as contact for now and try again."
> — Mike Cleaver, operator, at the moment the gap became obvious

> "Naturally, telephones now demand database ceremony before performing
> telephony."
> — Bob, the assistant, describing the previous state of affairs

### Availability

Ships with migration 357 on the `add-contacts` branch. Existing contacts,
groups, and inbound behaviour are unaffected; the first behaviour change is
the new tool in trusted sessions.

## FAQ

**Can a contact Bob created message him on WhatsApp?**
No. Agent-created contacts are outbound-only by construction
(`allow_inbound_dm = 0`). Message access is a separate dashboard decision.

**Can an untrusted person get Bob to add a contact?**
No. The tool exists only in trusted sessions. Untrusted contacts keep
search-only contact tools.

**What happens if Bob is asked to add a number that already exists?**
He gets back the existing contact — names and permissions untouched — and
carries on with the task.

**Does this change who can call Bob?**
No. Inbound calls and messages follow the same gates as before; this feature
only widens who Bob can call.

**Why keep contacts at all, rather than dialling raw numbers?**
Contacts are how Bob attaches a call to a person: transcripts, memory, and the
report back to the requesting chat all key off the contact record. A number
without a record is a call nobody can remember making.
