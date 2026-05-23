# Outreach

## Purpose and Intent

Outreach lets the Cyborg AI agent proactively initiate WhatsApp conversations with trusted contacts on behalf of a user, pursue a defined objective through that conversation, and relay results back to the originating session -- all without human involvement beyond the initial request.

The motivating use case: a trusted contact messages Cyborg asking "can you find out if John is free Thursday?" Cyborg opens a second conversation with John, negotiates the answer, reports back, and the original contact gets a reply.

This is a multi-session coordination mechanism. A single outreach operation spans two independent WhatsApp DM sessions: the requestor's session (where the ask originated) and the target's session (where the agent carries out the conversation). The bridge service detects outreach state on incoming messages and adjusts the agent's prompt and tool set accordingly, so the same LLM loop that handles ordinary messages also drives the outreach negotiation.

## Architecture

```
  Requestor (WhatsApp DM)             Target Contact (WhatsApp DM)
  +---------------------+             +---------------------+
  |  Session A           |             |  Session B           |
  |  agent:main:         |             |  agent:main:         |
  |  whatsapp:dm:AAAA    |             |  whatsapp:dm:BBBB    |
  |                      |             |                      |
  |  Tools available:    |             |  Tools available:    |
  |  - send_whatsapp_    |             |  - send_whatsapp_    |
  |    message           |             |    message           |
  |  - send_whatsapp_    |             |  - finish_outreach   |
  |    to_contact        |             |                      |
  |  - get_contact_      |             |                      |
  |    session_messages  |             |                      |
  +----------+-----------+             +----------+-----------+
             |                                    |
             |  1. "Ask John about Thursday"       |
             |  --> send_whatsapp_to_contact -->   |
             |       (validates trust, sends       |
             |        message, seeds route          |
             |        metadata, logs history)       |
             |                                    |
             |                           2. Target replies
             |                           --> incoming message
             |                               (bridge detects
             |                                outreach metadata
             |                                in route, injects
             |                                outreach prompt +
             |                                finish_outreach tool)
             |                                    |
             |                           3. Agent pursues objective
             |                              through conversation
             |                              then calls finish_outreach
             |                                    |
             |  <-- 4. Result dispatched --------  |
             |      (result stored as user msg     |
             |       in Session A, LLM invoked     |
             |       with send_whatsapp_message    |
             |       to relay answer to requestor) |
             +------------------------------------+


                    +----------------------+
                    |   WhatsApp Bridge     |
                    |   (Go companion)      |
                    |                      |
   Cyborg Server <--+   WebSocket          +--> WhatsApp API
   (Python/FastAPI) |   ws://host:8430/ws  |    (whatsmeow)
                    |                      |
                    +----------------------+
```

## Flow

### 1. Initiation

A trusted contact in a WhatsApp DM session asks the agent to reach out to someone else. The agent calls `send_whatsapp_to_contact` with:

- `contact_id` -- the target contact (must exist in contacts table)
- `message` -- the opening message to send
- `objective` -- what the agent needs to achieve, e.g. "Find out if John can meet Thursday and what time works"

The tool (`make_whatsapp_outreach_tools` in `whatsapp_outreach_tools.py`):

1. Validates the target contact exists and is trusted (`is_trusted = 1`)
2. Checks the WhatsApp bridge is connected
3. Sends the message via the bridge (`{phone_digits}@s.whatsapp.net`)
4. Creates or updates a session route for the target with outreach metadata:
   - `outreach_initiated_from` -- the requestor's session key
   - `outreach_objective` -- the stated goal
   - `outreach_requestor` -- the requestor's name
   - `outreach_message` -- the initial message text
5. Stores the sent message in the target session history as an assistant message
6. Upserts the target contact as a participant in the target session
7. Logs the outreach in both sessions' LLM call logs under `call_category="whatsapp_outreach"`

### 2. Target Conversation

When the target contact replies, the WhatsApp bridge delivers the incoming message as normal. During dispatch setup in `_handle_incoming_message` (in `whatsapp_bridge_service.py`), the bridge service:

1. Resolves the session key from the sender's JID
2. Looks up the session route's metadata
3. Detects `outreach_initiated_from` in the metadata
4. Injects an **outreach prompt** into the system message explaining the active objective and instructing the agent to call `finish_outreach` when done
5. Adds the `finish_outreach` tool to the agent's toolset via `make_outreach_reply_tools`

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

When the agent calls `finish_outreach(result)`:

1. Reads outreach metadata from the session route
2. Clears the outreach fields from the route's metadata
3. Builds a structured result message containing the target contact name, objective, requestor, and result text
4. Stores the result as a `user` message in the **requestor's session** (Session A)
5. Dispatches a new LLM call in the requestor's session, giving it the result plus a `send_whatsapp_message` tool to relay the answer back to the requestor via WhatsApp

If the agent doesn't explicitly call `send_whatsapp_message` during the result dispatch, the system auto-sends the LLM output as a fallback. This ensures the requestor always receives an answer.

The completion dispatch runs as a background `asyncio.Task` so it does not block the target session from continuing.

## Key Components

| Component | File | Role |
|---|---|---|
| Outreach tools | `services/whatsapp_outreach_tools.py` | `send_whatsapp_to_contact`, `get_contact_session_messages`, `finish_outreach` |
| WhatsApp bridge | `services/whatsapp_bridge_service.py` | WebSocket client to Go bridge; handles incoming messages, detects outreach state, injects outreach prompt + tools |
| Session routes | `services/session_route_service.py` | Routes map session keys to WhatsApp chats; route metadata carries outreach state |
| Session agenda | `services/session_agenda_service.py` | Determines system prompt based on trust level (unverified / known-untrusted / trusted) |
| Dispatch gate | `services/session_dispatch_gate.py` | Per-session asyncio lock ensuring only one LLM dispatch runs per session at a time |
| Prompt assembler | `services/prompt_assembler.py` | Builds the system prompt (workspace + agenda + participants + outreach + memory) and chat history |
| LLM dispatch | `services/llm_dispatch.py` | Routes LLM calls to OpenAI with tool support; logs all interactions to `llm_call_log` |

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

The trust tier is resolved in `_handle_incoming_message` by looking up the sender's phone number in the `contacts` table. Unknown WhatsApp senders are auto-seeded as unverified contacts with `is_trusted = 0`.

The `SessionAgendaService` uses the trust tier to select the appropriate system prompt:

| Tier | WhatsApp Agenda | Key Restrictions |
|---|---|---|
| Unverified | `WHATSAPP_DEFAULT_AGENDA` | No identity assumptions, no sensitive data, no link clicking |
| Known Untrusted | `WHATSAPP_KNOWN_UNTRUSTED_AGENDA` | No config changes, stay within conversation bounds |
| Trusted | `WHATSAPP_TRUSTED_AGENDA` | Full capabilities including outreach, contact search, delegation |

## Session Key Convention

Session keys follow the format `agent:{agent_id}:whatsapp:{kind}:{identifier}`:

- **DM**: `agent:main:whatsapp:dm:61412345678` (phone digits from sender JID)
- **Group**: `agent:main:whatsapp:group:abc123` (group ID before `@g.us`)

The phone-to-JID conversion (`_phone_to_jid`) strips non-digits and appends `@s.whatsapp.net`. The reverse (`_jid_to_phone`) normalizes to `+CC` format, defaulting to Australian country code (+61) when ambiguous.

The bridge derives session keys in `_handle_incoming_message` using the sender JID's numeric part for DMs and the group chat ID for groups. The outreach tools derive the target session key from the contact's phone number digits using the same `whatsapp:dm:` pattern.

## Data Model

The outreach state machine lives entirely in `session_routes.metadata` (JSON column). There is no separate outreach table.

```
session_routes
+-- id (TEXT PK)
+-- channel (TEXT)          -- "whatsapp"
+-- session_key (TEXT)      -- "agent:main:whatsapp:dm:61412345678"
+-- kind (TEXT)             -- "dm" | "group"
+-- contact_id (TEXT FK)    -- -> contacts.id
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

### Available in requestor session (trusted DM)

| Tool | Purpose |
|---|---|
| `send_whatsapp_to_contact` | Initiate outreach to another trusted contact |
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

The bridge service's `_handle_incoming_message` method controls which tools are available based on trust and outreach state:

```
Incoming WhatsApp message
         |
         v
  Resolve contact + trust tier
         |
         +-- All sessions get:
         |   - workspace tools
         |   - memory tools
         |   - send_whatsapp_message
         |   - send_whatsapp_media
         |
         +-- Trusted contacts also get:
         |   - contact tools (search_contacts, etc.)
         |   - outreach tools (send_whatsapp_to_contact, get_contact_session_messages)
         |   - reflection tools
         |   - delegation tools (if skill_dev_enabled)
         |
         +-- Session has outreach metadata?
             |
             +-- Yes: add finish_outreach tool
             +-- No: nothing extra
```

## Dispatch Lifecycle

Every LLM invocation goes through the `LLMDispatchService`. For WhatsApp messages, the dispatch runs as a background `asyncio.Task` gated by a per-session lock (`SessionDispatchGate`) to ensure only one dispatch runs per session at a time:

1. Incoming message arrives, a background task is created
2. The task acquires the session lock via `SessionDispatchGate.get_lock(session_key)`
3. It marks queued messages as dispatched (`mark_dispatched`) to enable batching
4. `LLMDispatchService.chat_with_tools` runs the LLM call with the assembled tools
5. On success, the LLM response is logged with latency and token usage
6. Auto-send fallback: if the LLM produced text but never called `send_whatsapp_message`, the text is sent anyway
7. The assistant response is recorded in session history

The same dispatch mechanism is used for both incoming WhatsApp messages (`call_category="whatsapp_incoming"`) and outreach result delivery (`call_category="outreach_result"`).

## System Prompt Assembly

The system prompt for each dispatch is assembled from multiple layers:

```
  +--------------------+
  | Workspace prompt   |  SOUL.md, IDENTITY.md, AGENTS.md, USER.md,
  |                    |  skills index, memory index (always-access wikis),
  |                    |  grounding rules
  +--------------------+
  | Agenda             |  Trust-tier-specific instructions
  |                    |  (unverified / known-untrusted / trusted)
  +--------------------+
  | Participants       |  Who is in this session (name, trust status)
  +--------------------+
  | Outreach prompt    |  Active outreach objective (if applicable)
  |                    |  + instruction to call finish_outreach
  +--------------------+
  | Trusted memory     |  Memory index for trusted-access wikis
  |                    |  (only injected for trusted contacts)
  +--------------------+
```

Each layer is conditionally included. The workspace prompt is cached and reloaded only when workspace files change on disk.

## Logging

Outreach actions are recorded in the unified LLM call log (`_record_log` in `llm_dispatch.py`) under both sessions:

- **Source session**: logs the outreach initiation with `call_category="whatsapp_outreach"`, recording the objective as the user message and the sent text as the response.
- **Target session**: logs the same outreach event with a prefix indicating the requestor, so it surfaces in the dashboard for both conversations.
