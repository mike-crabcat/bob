# Outreach

## Purpose and Intent

Outreach is the mechanism by which the Bob AI agent proactively initiates a WhatsApp conversation with a trusted contact on a user's behalf, pursues a defined objective through that conversation, and relays the results back to the session where the request originated. After the initial request the entire cycle runs autonomously -- no further human involvement is required.

The motivating use case: a trusted contact messages Bob asking "can you find out if John is free Thursday?" Bob opens a second conversation with John, negotiates the answer, reports back, and the original contact receives a reply.

This is a multi-session coordination mechanism. A single outreach operation spans two independent WhatsApp DM sessions: the requestor's session (where the ask originated) and the target's session (where the agent carries out the conversation). The bridge service detects outreach state on incoming messages and adjusts the agent's prompt and tool set accordingly, so the same LLM loop that handles ordinary messages also drives the outreach negotiation.

## Architecture

```
  Requestor (WhatsApp DM)              Target Contact (WhatsApp DM)
  +---------------------+              +---------------------+
  |  Session A           |             |  Session B           |
  |  agent:main:         |             |  agent:main:         |
  |  whatsapp:dm:AAAA    |             |  whatsapp:dm:BBBB    |
  |                      |             |                      |
  |  Tools available:    |             |  Tools available:    |
  |  - send_whatsapp_    |             |  - send_whatsapp_    |
  |    message           |             |    message           |
  |  - send_whatsapp_    |             |  - send_whatsapp_    |
  |    to_contact        |             |    media             |
  |  - get_contact_      |             |  - finish_outreach   |
  |    session_messages  |             |                      |
  |  - search_contacts   |             |                      |
  +----------+-----------+             +----------+-----------+
             |                                    |
             |  1. "Ask John about Thursday"      |
             |  --> send_whatsapp_to_contact -->  |
             |       (validates trust, sends      |
             |        message, seeds route        |
             |        metadata, logs history)     |
             |                                    |
             |              2. Target replies     |
             |              --> incoming message  |
             |                  (bridge detects   |
             |                   outreach         |
             |                   metadata in      |
             |                   route, injects   |
             |                   outreach prompt  |
             |                   + finish_outreach|
             |                   tool)            |
             |                                    |
             |              3. Agent pursues the  |
             |                 objective through  |
             |                 the conversation,  |
             |                 then calls         |
             |                 finish_outreach    |
             |                                    |
             | <-- 4. Result dispatched --------- |
             |     (result stored as user msg     |
             |      in Session A, LLM invoked     |
             |      with send_whatsapp_message    |
             |      to relay answer to requestor) |
             +------------------------------------+


                    +----------------------+
                    |   WhatsApp Bridge     |
                    |   (Go companion)      |
                    |                      |
   Bob Server  <----+   WebSocket          +--> WhatsApp API
   (Python/FastAPI) |   ws://host:8430/ws  |    (whatsmeow)
                    |                      |
                    +----------------------+
```

## Flow

### 1. Initiation

A trusted contact in a WhatsApp DM session asks the agent to reach out to someone else. The agent calls `send_whatsapp_to_contact` with:

- `contact_id` -- the target contact (must exist in the `contacts` table)
- `message` -- the opening message to send
- `objective` -- what the agent needs to achieve, e.g. "Find out if John can meet Thursday and what time works"
- `media_path` -- optional workspace-relative path to an image or media attachment

The tool is defined in `make_whatsapp_outreach_tools` (`packages/bob-server/bob_server/services/whatsapp_outreach_tools.py`) and performs these steps:

1. Validates the target contact exists and has a phone number.
2. Checks the WhatsApp bridge is connected.
3. Converts the phone number to a JID (`_phone_to_jid` strips non-digits and appends `@s.whatsapp.net`) and sends the message -- via `send_media` if `media_path` is provided, otherwise `send_message`. The media path is resolved within the workspace directory and rejected if it escapes.
4. Creates or updates the target session route with outreach metadata:
   - `outreach_initiated_from` -- the requestor's session key
   - `outreach_objective` -- the stated goal
   - `outreach_requestor` -- the requestor's name
   - `outreach_message` -- the initial message text
5. Stores the sent message in the target session history as an `assistant` message (Bob authored it) with `metadata.outreach = True`.
6. Upserts the target contact as a participant in the target session.
7. Logs the outreach in both sessions' LLM call logs under `call_category="whatsapp_outreach"` (see Logging below).

If a route already exists for the target session key, the `ConflictError` from `SessionRouteService.create_route` is caught and the existing route's `metadata` is updated by merging in the outreach fields.

### 2. Target Conversation

When the target contact replies, the WhatsApp bridge delivers the incoming message as normal. During dispatch setup in `_handle_incoming_message` (in `whatsapp_bridge_service.py`), the bridge service:

1. Resolves the session key from the sender's JID.
2. Looks up the session route's `metadata`.
3. Detects `outreach_initiated_from` in the metadata.
4. Injects an **outreach prompt** into the assembled system message, explaining the active objective and instructing the agent to call `finish_outreach` when done.
5. Adds the `finish_outreach` tool to the agent's toolset via `make_outreach_reply_tools`.

The outreach prompt appended to the system message reads:

```
## Active Outreach Request
You proactively sent a message to this contact.
- Requested by: {requestor_name}
- Objective: {outreach_objective}
- Your initial message: "{outreach_message}"

Your goal is to achieve the objective through this conversation.
When you have the information needed, call the finish_outreach tool to relay the result back.
```

The agent then has a normal conversation with the target, pursuing the objective. It can use `send_whatsapp_message` to reply and `finish_outreach` to signal completion.

### 3. Completion

When the agent calls `finish_outreach(result)` (defined in `make_outreach_reply_tools`):

1. Reads outreach metadata (`outreach_initiated_from`, `outreach_objective`, `outreach_requestor`) from the current session route.
2. Looks up the target contact's name for inclusion in the result.
3. Clears the outreach fields from the route's metadata (`outreach_initiated_from`, `outreach_objective`, `outreach_requestor`, `outreach_message`), setting `metadata` to NULL when no keys remain.
4. Builds a structured result message containing the target contact name, objective, requestor, and result text.
5. Stores the result as a `user` message in the **requestor's session** (Session A) with `metadata.outreach_result = True`.
6. Dispatches a new LLM call (`call_category="outreach_result"`) in the requestor's session, with workspace tools plus a `send_whatsapp_message` tool that targets the requestor's chat.
7. If the agent produces a non-empty response but does not invoke `send_whatsapp_message`, the **tap dispatch** mechanism (`tap_dispatch` in `services/tap.py`) runs a follow-up LLM call reminding the agent that the send tool must be used. This ensures the requestor always receives an answer.
8. The assistant response is recorded in the requestor session's history (unless it is a `NO_REPLY` variant).

The completion dispatch runs as a background `asyncio.Task` so it does not block the target session from continuing. The requestor session's lock (`SessionDispatchGate`) is acquired as normal, so the result dispatch serialises against any other activity in that session.

## Key Components

| Component | File | Role |
|---|---|---|
| Outreach tools | `services/whatsapp_outreach_tools.py` | `send_whatsapp_to_contact`, `get_contact_session_messages`, `finish_outreach` |
| WhatsApp bridge | `services/whatsapp_bridge_service.py` | WebSocket client to the Go bridge; handles incoming messages, detects outreach state, injects the outreach prompt and reply tools |
| Tool registry | `services/tool_registry.py` | `build_common_tools()` assembles shared tools; channel-specific tools (outreach, `send_whatsapp_message`) are added by the bridge |
| Session routes | `services/session_route_service.py` | Routes map session keys to channels/chats; route `metadata` carries outreach state |
| Session agenda | `services/session_agenda_service.py` | Selects the system prompt based on trust tier (unverified / known-untrusted / trusted) |
| Dispatch gate | `services/session_dispatch_gate.py` | Per-session `asyncio.Lock` ensuring only one LLM dispatch runs per session at a time |
| Prompt assembler | `services/prompt_assembler.py` | `load_workspace_prompt` and `build_chat_messages` used by the result dispatch |
| Tap dispatch | `services/tap.py` | Follow-up LLM call when the agent produced text but did not use its send tool |
| LLM dispatch | `services/llm_dispatch.py` | Routes LLM calls to the model with tool support; `_record_log` persists all interactions to `llm_call_log` |

All paths under `packages/bob-server/bob_server/`.

## Trust Model

Outreach is only available to **trusted** contacts. Three trust tiers control agenda and tool access:

```
                  +-----------------------------------------+
  Unverified      |  Caution agenda. No contact tools.       |
  (no contact)    |  Cannot initiate or receive outreach.    |
                  +-----------------------------------------+
                        |
                        v
                  +-----------------------------------------+
  Known Untrusted |  Restricted agenda. Cannot modify        |
  (contact,       |  config or share sensitive data.         |
  is_trusted=0)   |  Cannot initiate outreach.               |
                  +-----------------------------------------+
                        |
                        v
                  +-----------------------------------------+
  Trusted         |  Full agenda. Contact tools + outreach   |
  (contact,       |  tools + reflection + delegation.        |
  is_trusted=1)   |  Can initiate and receive outreach.      |
                  +-----------------------------------------+
```

The trust tier is resolved in `_handle_incoming_message` by looking up the sender's phone number in the `contacts` table. Unknown WhatsApp senders are auto-seeded as unverified contacts with `is_trusted = 0`. The `_resolve_or_seed_contact` method also performs prefix matching on phone numbers to handle JIDs with extra trailing digits (e.g. `+614154068544` matching `+61415406854`).

The `SessionAgendaService.get_effective_agenda` uses the trust tier to select the appropriate WhatsApp system prompt:

| Tier | WhatsApp Agenda | Key Restrictions |
|---|---|---|
| Unverified | `WHATSAPP_DEFAULT_AGENDA` | No identity assumptions, no sensitive data, no link clicking |
| Known Untrusted | `WHATSAPP_KNOWN_UNTRUSTED_AGENDA` | No config changes, stay within conversation bounds |
| Trusted | `WHATSAPP_TRUSTED_AGENDA` | Full capabilities including outreach, contact search, subagents |

Note: outreach tools (`send_whatsapp_to_contact`, `get_contact_session_messages`) are also injected for **group** sessions where at least one participant is trusted, enabling outreach from group conversations where a trusted participant makes a request. The injection condition in `_handle_incoming_message` is `contact_id and (is_trusted or chat_kind == "group")`.

## Session Key Convention

Session keys follow the format `agent:{agent_id}:whatsapp:{kind}:{identifier}`:

- **DM**: `agent:main:whatsapp:dm:61412345678` (phone digits from sender JID)
- **Group**: `agent:main:whatsapp:group:abc123` (group ID before `@g.us`)

Phone-to-JID conversion (`_phone_to_jid`) strips non-digits and appends `@s.whatsapp.net`. The reverse (`_jid_to_phone`) normalises to `+CC` format, defaulting to the Australian country code (`+61`) when no country code is detectable.

The bridge derives session keys in `_handle_incoming_message` using the sender JID's numeric part for DMs and the group chat ID for groups. The outreach tools derive the target session key from the contact's phone digits using the same `whatsapp:dm:` pattern, ensuring it matches what the bridge will compute when the target later replies.

## Data Model

The outreach state machine lives entirely in `session_routes.metadata` (a JSON column). There is no separate outreach table.

```
session_routes
+-- id (TEXT PK)
+-- channel (TEXT)          -- "whatsapp"
+-- session_key (TEXT)      -- "agent:main:whatsapp:dm:61412345678"
+-- kind (TEXT)             -- "dm" | "group" | "thread"
+-- contact_id (TEXT FK)    -- -> contacts.id  (set for DMs)
+-- chat_id (TEXT)          -- set for groups/threads; NULL for DMs
+-- metadata (TEXT JSON)    -- {outreach_initiated_from, outreach_objective, ...}
+-- is_active (INT)
```

Outreach lifecycle as seen in metadata:

```
  No outreach state         Active outreach              Outreach complete
  +--------------+         +----------------------+     +------------------+
  | { ... }      |  --->   | {                    |     | { ... }          |
  |              |  send   |   outreach_initiated |     |  (fields popped) |
  |              |         |   _from: "agent:...",|     |                  |
  |              |         |   outreach_objective |     |                  |
  |              |         |   outreach_requestor |     |                  |
  |              |         |   outreach_message   |     |                  |
  +--------------+         | }                    |     +------------------+
                           +----------------------+
                              |
                              |  finish_outreach called
                              |  (fields removed)
                              v
```

## Tool Inventory

### Available in requestor session (trusted DM, no active incoming outreach)

| Tool | Purpose |
|---|---|
| `send_whatsapp_to_contact` | Initiate outreach to another contact |
| `get_contact_session_messages` | Check messages in another contact's session |
| `send_whatsapp_message` | Reply in current conversation |
| `send_whatsapp_media` | Send an image in current conversation |
| `search_contacts` | Look up contacts by name/phone/email |

### Available in target session (active outreach)

| Tool | Purpose |
|---|---|
| `send_whatsapp_message` | Reply to the target contact |
| `send_whatsapp_media` | Send an image to the target contact |
| `finish_outreach` | Complete outreach and relay result to requestor |

### Available during result dispatch (requestor session)

| Tool | Purpose |
|---|---|
| `send_whatsapp_message` | Relay the outreach result to the requestor |

## Tool Injection Points

The bridge service's `_handle_incoming_message` method controls which tools are available based on trust and outreach state. Tools are assembled in two stages: `build_common_tools()` from `tool_registry.py` provides the shared set, then the bridge adds channel-specific tools.

```
Incoming WhatsApp message
         |
         v
  Resolve contact + trust tier
         |
         +-- build_common_tools() provides:
         |   - workspace tools
         |   - memory tools
         |   - docs tools
         |   - changelog tools
         |   - email_send tools
         |
         +-- Trusted contacts also get:
         |   - contact tools (search_contacts, etc.)
         |   - reflection tools
         |   - subagent tools (if skill_dev_enabled)
         |
         +-- Trusted DMs or group sessions with a trusted contact:
         |   - outreach tools (send_whatsapp_to_contact,
         |     get_contact_session_messages)
         |
         +-- Channel-specific (always):
         |   - send_whatsapp_message
         |   - send_whatsapp_media
         |
         +-- Session route has outreach metadata?
             |
             +-- Yes: add finish_outreach tool
                    + inject outreach prompt into system message
             +-- No: nothing extra
```

## Dispatch Lifecycle

Every LLM invocation goes through `LLMDispatchService`. For WhatsApp messages, the dispatch runs as a background `asyncio.Task` gated by a per-session lock (`SessionDispatchGate`) to ensure only one dispatch runs per session at a time:

1. Incoming message arrives; a background task is created via `asyncio.create_task`.
2. The task acquires the session lock via `SessionDispatchGate.get_lock(session_key)`.
3. It marks queued messages as dispatched (`SessionService.mark_dispatched`) to enable batching of rapid messages.
4. `LLMDispatchService.chat_with_tools` runs the LLM call with the assembled tools.
5. On success, the LLM response is logged with latency and token usage via `_record_log`.
6. **Tap fallback**: if the LLM produced a non-empty text response but never called `send_whatsapp_message`, a second LLM call (`tap_dispatch`) is made, appending a reminder that the send tool must be used.
7. The assistant response is recorded in session history.
8. If the response is a `NO_REPLY` variant, it is not recorded to avoid poisoning future context.

The same dispatch mechanism is used for incoming WhatsApp messages (`call_category="whatsapp_incoming"`) and the outreach result delivery (`call_category="outreach_result"`). The result dispatch runs from inside `finish_outreach` and follows the same gating, logging and tap fallback rules.

## System Prompt Assembly

The system prompt for each WhatsApp dispatch is assembled by the bridge service from multiple layers:

```
  +--------------------+
  | Workspace prompt   |  SOUL.md, IDENTITY.md, AGENTS.md, USER.md,
  |                    |  skills index, memory index, grounding rules
  +--------------------+
  | Participants       |  Who is in this session (name, trust status)
  +--------------------+
  | Person profile     |  Memory profile for the DM contact (DM sessions only,
  |                    |  via MemoryService.find_person_entry)
  +--------------------+
  | Group memory hint  |  Memory hint summarising trusted group members
  |                    |  (group sessions only)
  +--------------------+
  | Outreach prompt    |  Active outreach objective (if applicable)
  |                    |  + instruction to call finish_outreach
  +--------------------+
```

Each layer is conditionally included. The workspace prompt is cached and reloaded only when workspace files change on disk (tracked via `st_mtime`). The outreach layer is appended only when the session route's metadata contains `outreach_initiated_from`.

## Logging

Outreach actions are recorded in the unified LLM call log (`_record_log` in `llm_dispatch.py`) under both sessions:

- **Requestor session** (`current_session_key`): logs the outreach initiation with `call_category="whatsapp_outreach"`, recording `Reach out to {name}: {objective}` as the user message and the sent text as the response.
- **Target session** (`target_session_key`): logs the same outreach event with a prefix indicating the requestor (`[Outreach initiated - requested by {requestor}]`), so it surfaces in the dashboard for both conversations.

Both log entries use `provider="outreach"` and `status="completed"` so the dashboard's session view can show proactive outreach alongside ordinary incoming-message dispatches. The dashboard API (`routers/dashboard_api.py`) explicitly includes sessions that only have messages and no `llm_call_log` rows (e.g. newly seeded outreach targets) when listing sessions.
