# Session data model is messy

## Problem

Group chat identity is split across two tables with a soft-FK and no integrity constraint:

- `session_routes` — per-agent routing: `session_key`, `channel`, `kind`, `chat_id` (JID for groups), `contact_id` (for DMs)
- `whatsappgroups` — channel-specific metadata: `whatsapp_jid`, `name`, `member_count`, `memory_entity_id`

They're joined by convention (`session_routes.chat_id = whatsappgroups.whatsapp_jid`), not a real FK.

### Symptoms

1. **Asymmetric identity paths by chat kind.** DMs resolve through `contacts`; groups resolve through `whatsappgroups`. Any code that maps `session_key → entity` has to branch on `kind` everywhere.
2. **Race window.** `session_routes` row is created on first inbound group message (`whatsapp_bridge_service.py:1318-1343`); `whatsappgroups` row is populated later, when a `GroupSync` event arrives from the bridge (`whatsapp_bridge_service.py:636-642`). In between, the session exists with no display name.
3. **Redundant identity.** `session_key` already embeds the JID (`agent:main:whatsapp:group:120363423288899302`). `chat_id` carries it again. No single source of truth.
4. **Memory linkage is asymmetric.** DMs link to memory via `contacts`; groups link via `whatsappgroups.memory_entity_id`. Two entry points into the memory graph for what is conceptually one relationship (session ↔ memory entity).
5. **Soft-FK allows orphans.** Nothing prevents a `session_routes` row whose `chat_id` doesn't match any `whatsappgroups` row, or vice versa.

## Proposed cleanup (not started)

Unify into a single `channels` (or `conversations`) table owning identity for both DMs and groups:

- `id`, `type` (dm/group/thread/call), `display_name`, `memory_entity_id`, `metadata`
- Both DMs and groups live here; `contacts` becomes purely people, not routing targets.

`session_routes` becomes a thin per-agent routing layer that references `channels.id`:

- `agent_id`, `channel_id` (FK → channels), `session_key`, `is_active`

This collapses the kind-branching in resolvers, removes the race window (channel row created first, session route attached), and gives memory one consistent entry point regardless of chat type.

## Cost

Meaningful refactor:

- Migration to introduce `channels`, backfill from `contacts` (DM rows) + `whatsappgroups` (group rows), rewrite `session_routes` to reference `channels.id`.
- Every resolver that branches on `kind` to look up name/entity: `session_tools.py:80-92`, dashboard API, prompt assembler, etc.
- Memory entity linkage (`ensure_group_entity` in `memory/service.py:1068-1109`, contact-side equivalent) needs to write to `channels.memory_entity_id` instead of two different tables.
- Bridge event handling (`whatsapp_bridge_service.py` sync/member-change paths) needs to write to `channels` instead of `whatsappgroups`.

Worth doing only if the branching is actively biting — e.g. if adding a new channel type (email threads, Slack) forces another parallel table and another branch in every resolver.
