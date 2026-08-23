# Outreach

## Purpose and Intent

Outreach is the mechanism by which the Bob AI agent proactively initiates a conversation with a third party on a user's behalf, pursues a defined objective through that conversation, and relays the result back to the conversation where the request originated. After the initial request the cycle runs autonomously — no further human involvement is required.

The motivating use case: a trusted contact messages Bob asking "can you find out if John is free Thursday?". Bob opens a second WhatsApp DM with John, negotiates the answer, reports back, and the original contact receives a reply.

This is a multi-conversation coordination mechanism. A single outreach operation spans two independent WhatsApp sessions:

- **The origin session** (requestor) — where the ask originated. Bob relays the final result back here.
- **The target session** — the contact Bob reaches out to. The same LLM loop that handles ordinary messages also drives the outreach negotiation, but with an outreach objective injected into its system prompt and a `finish_outreach` tool added to its toolset.

Since the original implementation, outreach has been re-based on the Bob3 **goals + wake** substrate. An outreach is now modelled as a *goal* (`goals.kind='outreach'`) held by the target conversation on behalf of the origin conversation, with a 24-hour deadline wakeup that resurfaces unanswered outreach in the origin conversation. Result relay goes through the channel-agnostic **wake path** rather than a bespoke WhatsApp-only dispatch. The same substrate carries the sibling delegation flows (subagents, phone calls, email threads — see "Related Paths" below).

## Architecture

```
   Requestor (WhatsApp DM)                Target Contact (WhatsApp DM)
   +----------------------+              +----------------------+
   | Session A (origin)    |             | Session B (target)   |
   | agent:main:whatsapp:  |             | agent:main:whatsapp: |
   | dm:61412345678        |             | dm:61498765432       |
   +----------+-----------+             +----------+-----------+
              |                                    ^
              | 1. "Ask John about Thursday"        |
              |    LLM calls send_whatsapp_to_     |
              |    contact(contact_id, message,    |
              |    objective)                      |
              |                 +------------------+
              |                 |  - validate contact + bridge
              |                 |  - send message (WhatsApp)
              |                 |  - create goal (kind='outreach',
              |                 |    deadline = +24h, origin = A)
              |                 |  - seed route metadata:
              |                 |      outreach_initiated_from=A
              |                 |      outreach_objective, ...
              |                 |  - store opening msg in B
              v                 v
      goals table           session_routes.metadata
   (origin_conversation_id=A)  (active-outreach marker)
              |                                    |
              |                          2. Target replies
              |                          bridge detects outreach
              |                          metadata in route ->
              |                          "Active Outreach Request"
              |                          block in system prompt +
              |                          finish_outreach tool
              |                                    |
              |                          3. Agent converses,
              |                             pursues objective,
              |                             calls finish_outreach(result)
              |                                    |
              |   4a. settle_goal(completed)  <----+  (CAS transition,
              |       -> wakes origin with            cancels the 24h
              |          "Goal completed"            deadline wakeup)
              |       (fallback if no goal:
              |        wake_conversation directly)
              v                                    |
   +------------------------------------------------------------+
   | wake_conversation (services/wake_service.py)                |
   |  - store result as UNDISPATCHED user message in Session A   |
   |    (crash-safe: startup sweep re-arms it)                   |
   |  - bridge.wake_session(A) -> full inbound dispatch spec:    |
   |    attention coordinator -> DispatchRunner -> LLM           |
   |  - Session A turn gets send_whatsapp_message and texts the  |
   |    requestor the answer                                     |
   +------------------------------------------------------------+

   24h deadline (unanswered):  wakeup pump -> wake_conversation(A)
   with "Goal deadline reached ... decide how to proceed"
```

The WhatsApp transport itself:

```
                    +----------------------+
                    |  WhatsApp Bridge      |
                    |  (Go companion,       |
                    |   whatsmeow)          |
   Bob Server  <----+  WebSocket           +--> WhatsApp
   (Python/FastAPI) |  ws://host:8430/ws   |
                    +----------------------+
```

## Flow

### 1. Initiation — `send_whatsapp_to_contact`

The tool is defined in `make_whatsapp_outreach_tools` (`services/whatsapp_outreach_tools.py`). Parameters: `contact_id`, `message` (the opening text), `objective` (what the agent must achieve, e.g. "Find out if John can meet Thursday and what time works"), and optional `media_path` (workspace-relative attachment).

Steps performed:

1. Loads the contact; fails if missing or has no phone number.
2. Checks the WhatsApp bridge is connected.
3. Converts the phone number to a JID (`_phone_to_jid`: strip non-digits, append `@s.whatsapp.net`) and sends — `send_media` (after `_prepare_media`; the media path is resolved within the workspace and rejected if it escapes) when `media_path` is given, otherwise `send_message`.
4. Derives the target session key `agent:main:whatsapp:dm:{phone_digits}` — exactly what the bridge will compute when the target replies.
5. Resolves the requestor's display name from the current session route's contact.
6. **Creates a goal** (`goal_service.create_goal`, Bob3 Phase V): `kind="outreach"`, `conversation_id` = target session, `origin_conversation_id` = current session, `deadline` = now + 24h. A deadline schedules a wakeup for the origin conversation so unanswered outreach resurfaces. Failures are logged and swallowed — outreach still works without the goal (legacy path).
7. Creates or updates the target session route with outreach metadata (see Data Model). If the route already exists, the `ConflictError` from `SessionRouteService.create_route` is caught and the outreach fields are merged into the existing `metadata` JSON.
8. Stores the sent message in the target session history as an `assistant` message (Bob authored it) with `metadata = {"outreach": true, "objective": ..., "requestor": ...}`.
9. Upserts the target contact as a (trusted) participant of the target session.
10. Logs the event in **both** sessions' LLM call logs (see Logging).

### 2. Target conversation

When the target replies, the bridge delivers the message through the normal inbound pipeline (`_handle_incoming_message` in `services/whatsapp_bridge_service/_service.py`): sender resolution/trust, conversation canonicalisation, participant upsert, route ensure, message + ingress event stored in one transaction, then the attention coordinator gates when the dispatch runs.

The dispatch spec (`_build_inbound_dispatch_spec`) detects outreach state on the session route:

- `ContextAssembler.outreach_prompt(session_key)` (`services/context_assembler.py`) checks the route metadata for `outreach_initiated_from` and, if present, appends this block to the system message:

  ```
  ## Active Outreach Request
  You proactively sent a message to this contact.
  - Requested by: {outreach_requestor}
  - Objective: {outreach_objective}
  - Your initial message: "{outreach_message}"

  Your goal is to achieve the objective through this conversation. When you have
  the information needed, call the finish_outreach tool to relay the result back.
  ```

- `make_outreach_reply_tools` adds the `finish_outreach` tool.

The agent then converses normally — replying via `send_whatsapp_message`, using workspace/memory/docs tools as needed — until the objective is met.

### 3. Completion — `finish_outreach(result)`

1. Reads the outreach metadata from the current session route; returns an error if no outreach is active.
2. Resolves the objective, requestor, `outreach_goal_id`, and the target contact's name.
3. Clears the five outreach fields from the route metadata (`outreach_initiated_from`, `outreach_objective`, `outreach_requestor`, `outreach_message`, `outreach_goal_id`), setting `metadata` to NULL when no keys remain.
4. Builds a structured result: `## Outreach Result` with contact, objective, requestor and the result text.
5. **Goal path**: `settle_goal(status="completed", result=...)`. The status change is a CAS on `active` (exactly one settler wins), which cancels the outstanding wakeups, appends a `goal.completed` event to the event log, and — because the goal's origin differs from its working conversation — wakes the origin conversation with `call_category="goal_result"`.
6. **Legacy fallback** (no goal, or settle failed): `wake_conversation(origin, result, call_category="outreach_result", metadata={"outreach_result": true, "source_session": ...})` directly.

`finish_outreach` itself does not dispatch an LLM call — the result relay is entirely the wake path's job (below). Because the tool call runs inside the target session's dispatch, this ordering means the origin's turn starts concurrently with the target's turn finishing.

### 4. Result relay — the wake path

`wake_conversation` (`services/wake_service.py`) is the single channel-agnostic entry point:

1. Resolves the conversation's channel (via `conversations`/`bindings`, preferring a WhatsApp binding as it has the richest pipeline).
2. Stores the content as a **user** message with `dispatched=0` in the origin session — durable, so a crash before dispatch is recovered by the bridge's startup sweep (`resume_pending_sessions`, run ~10s after the bridge connects).
3. For WhatsApp, calls `bridge.wake_session(session_key)`, which rebuilds the full inbound dispatch spec (system prompt with all layers, tools including `send_whatsapp_message`) and submits it through `AttentionCoordinator.resume_pending`. The origin's turn therefore runs through exactly the same hardened pipeline as a live inbound message: session lock, turn claim, effects-based sends, delivered-only history.
4. Non-WhatsApp origins get a generic workspace-tools turn (`_generic_wake_dispatch`).

The origin turn receives the result text as new context and — typically — calls `send_whatsapp_message` to text the answer to the requestor. If it produces text but never calls the send tool, the **tap** fallback (`services/tap.py`) gives it one reminder turn — but only when `BOB_ENABLE_TAP` is set (off by default; nothing sent means nothing recorded).

### 5. Deadline wakeup — unanswered outreach

At initiation, the 24h deadline schedules a wakeup row for the **origin** conversation. The heartbeat's `WakeupPumpTask` (`heartbeat.py`) runs `pump_due_wakeups` every tick; a due wakeup whose goal is still `active` wakes the origin with:

```
## Goal deadline reached
Objective: {objective}
Status: still active (no result yet)
Progress: {progress or 'none recorded'}

Decide how to proceed: follow up, revise the goal, or report back.
```

(`call_category="goal_deadline"`). The goal the agent sees is also visible via `list_goals`, and it can `update_goal` to record progress along the way. Settling the goal (via `finish_outreach`, `complete_goal`, or cancellation) cancels the outstanding wakeup.

## Dispatch Pipeline

Every LLM invocation for WhatsApp runs through `DispatchRunner` (`services/dispatch_runner.py`) driven by a `DispatchSpec`. For inbound messages, the **attention coordinator** (`services/attention/`) decides *when* the dispatch runs, not a per-message task:

```
  incoming message
        |
  store user message + append event_log row  (one transaction)
        |
  occupancy check: live call on this conversation?
        |-- yes, not urgent --> defer; post-call drain (wake_session)
        |                        runs queued texts as one turn
        v
  AttentionCoordinator.submit
        |-- Tier 0: addressed?  (all DMs are addressed; group needs
        |                        @mention / direct question)
        |-- Tier 1: debounce window (2.5s addressed, 20s group chatter;
        |           typing indicators extend; 90s hard cap)
        |-- Tier 2: actionability probe for unaddressed group batches
        |           (ACT / STAND_DOWN / WAIT; kill switch
        |            BOB_ATTENTION_ALWAYS_ACT=1)
        v
  DispatchRunner.run(spec)
        |-- acquire SessionDispatchGate lock (one dispatch per session)
        |-- claim pending user messages (mark_dispatched)
        |-- claim a durable turn (turns table, leased)
        |-- build_chat_messages (system + delivered-only history)
        |-- LLMDispatchService.chat_with_tools
        |      |-- quota exhausted -> restore claimed messages,
        |      |                    one-line "out of credit" notice
        |      |                    (rate-limited to 1/hour/session)
        |-- tap second-chance (BOB_ENABLE_TAP only)
        |-- record assistant history (delivered_only policy:
        |      only texts actually passed to send_whatsapp_message
        |      enter history; nothing sent -> nothing recorded)
        |-- complete the turn; publish event
```

Outbound sends (including the origin turn's relay to the requestor) go through the **effects outbox** (`services/effects.py`): `emit_and_deliver(kind="whatsapp_send", idempotency_key=...)` records the effect durably before delivery, delivers inline via the bridge, and is retried by the heartbeat's `EffectPumpTask` after a crash. Idempotency keys (`whatsapp_send:{dispatch_id}:{seq}`) make retries safe. Media sends use `kind="whatsapp_send_media"`.

## Trust Model

Outreach initiation is gated on trust. The trust tier is resolved per inbound message by `WhatsAppInboundPolicy.resolve_sender` (`services/channel_policies.py`):

```
  +----------------------------------------------------------+
  | Unknown DM sender (no contact row)                        |
  | -> DROPPED (security gate). Group sync auto-seeds         |
  |    contacts for everyone Bob has seen in a group, so any  |
  |    legitimate acquaintance already has a row.             |
  +----------------------------------------------------------+
                          |
                          v
  +----------------------------------------------------------+
  | Known contact, is_trusted=0  ("known untrusted")          |
  | -> accepted; restricted agenda (no config changes, no     |
  |    sensitive data); NO outreach tools in DMs.             |
  +----------------------------------------------------------+
                          |
                          v
  +----------------------------------------------------------+
  | Known contact, is_trusted=1  ("trusted")                  |
  | -> full agenda; outreach tools, goal tools, voice tools,  |
  |    contact tools, reflection, subagents.                  |
  +----------------------------------------------------------+
```

- Unknown **group** senders are auto-seeded as untrusted contacts and accepted; unknown **DM** senders are dropped outright. Contacts with `allow_inbound_dm=0` (e.g. agent-created call targets) are dropped from DMs too.
- `SessionAgendaService.get_effective_agenda` selects the WhatsApp system prompt by tier: `WHATSAPP_DEFAULT_AGENDA` (no contact), `WHATSAPP_KNOWN_UNTRUSTED_AGENDA`, `WHATSAPP_TRUSTED_AGENDA`. The trusted agenda explicitly documents the outreach capability ("If asked to contact someone, use search_contacts ... then send_whatsapp_to_contact").
- Outreach tool injection in `_build_inbound_dispatch_spec` is `contact_id and (is_trusted or chat_kind == "group")`: trusted senders in DMs, and any contact-resolved sender in a group chat. So a trusted participant can trigger outreach from a group conversation.

## Session Keys and Conversations

Session keys follow `agent:{agent_id}:whatsapp:{kind}:{identifier}`:

- **DM**: `agent:main:whatsapp:dm:61412345678` (phone digits from the JID)
- **Group**: `agent:main:whatsapp:group:abc123` (group id before `@g.us`)

The outreach tools derive the target session key from the contact's phone digits using the same pattern, guaranteeing it matches what the bridge computes when the target replies.

Since Bob3 Phase VI, the channel-derived key is a *binding*: on every inbound message the bridge calls `ConversationRepository.ensure(session_key)` and everything downstream (participants, routes, messages, events, dispatch, wake) keys under the canonical `conversations.id`. Today the mapping is 1:1 (`ensure()` backfills `id = session_key`); it diverges only when conversations are merged, in which case outreach state and results land on the survivor conversation. Phone normalisation (`_jid_to_phone` → `services/phone_utils.normalize_phone`) canonicalises to `+CC` format.

## Data Model

There is no dedicated outreach table. Active-outreach state lives in `session_routes.metadata` (JSON); the durable lifecycle lives in the Bob3 tables.

```
session_routes                       goals (kind='outreach')
+-- id (TEXT PK)                     +-- id (TEXT PK)
+-- channel ("whatsapp")             +-- conversation_id      <- target session
+-- session_key                      +-- origin_conversation_id <- origin session
+-- kind ("dm"|"group")              +-- kind ("outreach")
+-- contact_id (FK -> contacts)      +-- objective
+-- chat_id                          +-- status (active|completed|failed|cancelled)
+-- metadata (TEXT JSON) <-- active  +-- version (optimistic CAS)
|     outreach marker                +-- deadline (+24h at creation)
|                                     +-- external_ref
v                                     +-- result (set on settle)
{ "outreach_initiated_from": ...,    +-- goal_transitions (history)
    "outreach_objective": ...,        "outreach_goal_id": ... }
    "outreach_requestor": ...,  }    wakeups
                                      +-- conversation_id <- origin
                                      +-- goal_id, not_before (+24h)
                                      +-- status (scheduled|fired|cancelled)

  event_log (append-only)            effects (outbox)
  +-- goal.created / goal.completed  +-- whatsapp_send / whatsapp_send_media
  +-- message.received (ingress)     +-- idempotency_key, status
```

Outreach lifecycle in the route metadata:

```
  No outreach state          Active outreach                    finish_outreach
  +--------------+   send_whatsapp_to_contact   +------------------------+
  | { ...other   | ---------------------------> | outreach_initiated_from|
  |   keys... }  |                              | outreach_objective     |
  |              |                              | outreach_requestor     |
  |              |                              | outreach_message       |
  |              |                              | outreach_goal_id       |
  +--------------+                              +------------------------+
        ^                                                  |
        |   finish_outreach pops all five keys             |
        +--------------------------------------------------+
        (metadata set to NULL when no keys remain)
```

## Tool Inventory

### Origin session (trusted WhatsApp sender)

| Tool | Source | Purpose |
|---|---|---|
| `send_whatsapp_to_contact` | `whatsapp_outreach_tools` | Initiate outreach (message + objective) |
| `get_contact_session_messages` | `whatsapp_outreach_tools` | Check whether a contact has replied |
| `create_goal` / `update_goal` / `complete_goal` / `list_goals` | `goal_tools` | General goal tracking (outreach goals appear here) |
| `initiate_voice_call` | `voice_outreach_tools` | Offer a browser voice-link in the current DM |
| `send_whatsapp_message` | bridge dispatch spec | Reply in the current conversation (single tool; optional `media_path`) |
| `search_contacts` and other contact tools | `contact_tools` | Find the target contact |

### Target session (active outreach)

| Tool | Purpose |
|---|---|
| `send_whatsapp_message` | Converse with the target contact |
| `finish_outreach` | Settle the outreach and relay the result to the origin |
| common tool set | Workspace, memory, docs, etc. as usual |

### Tool injection points

`_build_inbound_dispatch_spec` assembles tools in layers — `build_common_tools()` (`tool_registry.py`) provides the shared set, then the bridge adds channel-specific tools:

```
Incoming WhatsApp message
         |
   resolve sender (WhatsAppInboundPolicy) -> contact_id, is_trusted
         |
   build_common_tools():  workspace, memory, docs, changelog,
   |                      email_send, email threads, session tools,
   |                      routines (+ dream tools if enabled)
   |
   +-- trusted: contact tools, phone tools, reflection, subagents
   |
   +-- contact_id and (trusted or group):
   |      send_whatsapp_to_contact, get_contact_session_messages
   |
   +-- trusted: goal tools (create/update/complete/list)
   |
   +-- trusted: initiate_voice_call (self-gates to DM-only)
   |
   +-- group chats: group tools
   |
   +-- always: send_whatsapp_message (text + optional media_path)
   |
   +-- route metadata has outreach_initiated_from?
          yes -> finish_outreach + "Active Outreach Request" in system prompt
```

The system prompt is layered similarly: workspace prompt (SOUL.md/IDENTITY.md/AGENTS.md/skills/memory indexes), participants, person profile (DMs), group memory hint (groups), dream plans, then the outreach block.

## Related Paths (same goal/wake substrate)

Outreach is one instance of a general pattern — a goal held by one conversation on behalf of another, settled with a wake-back:

| Flow | Initiation | Working-conversation signal | Completion |
|---|---|---|---|
| WhatsApp outreach | `send_whatsapp_to_contact` | route metadata + outreach prompt | `finish_outreach` → settle goal (kind `outreach`) |
| Email thread | `email_send(to, subject, body, agenda)` records `origin_session_key` on the thread | "Active Thread Task" prompt + `finish_email_thread` injected by the email poller | `finish_email_thread` → `wake_conversation` (`email_thread_result`) |
| Voice (task-oriented) | `create_subagent(agent_type="openai_voice", modality="phone"\|"voice_link")` | subagent goal (kind `subagent`/`call`), `external_ref` = subagent id | call summary → `phone_call_result_service` settles goal and wakes origin |
| Subagents | `create_subagent(task)` | goal with `origin_conversation_id` = parent | first result settles goal, wakes parent |

`initiate_voice_call` (in `voice_outreach_tools.py`) is a lighter sibling: it offers the *current* DM contact a browser voice-link (a `voice_sessions` row mirrored into `phone_calls`), sharing memory with the chat. It is not a cross-conversation delegation. The older `reach_out_with_voice_call` tool was retired (2026-08-14): its phone branch duplicated the subagent path and its defaults biased the LLM toward links when a real phone call was asked for.

## Logging and Observability

Outreach events are recorded in the unified LLM call log (`llm_call_log` via `_record_log` in `llm_dispatch.py`):

- **Origin session**: `call_category="whatsapp_outreach"`, user message `Reach out to {name}: {objective}`, response = the sent text.
- **Target session**: same category, prefixed `[Outreach initiated — requested by {requestor}]`.
- Both rows use `provider="outreach"`, `status="completed"`.

Result delivery appears as ordinary dispatches: `goal_result` (goal path) or `outreach_result` (legacy fallback) in the origin session, and `goal_deadline` when the 24h wakeup fires. Goal lifecycle events (`goal.created`, `goal.completed`) are appended to the append-only `event_log`. The dashboard session list (`routers/dashboard_api/sessions.py`) includes sessions that have messages but no `llm_call_log` rows, so freshly seeded outreach targets are visible immediately.

## Key Components

All paths under `packages/bob-server/bob_server/`.

| Component | File | Role |
|---|---|---|
| Outreach tools | `services/whatsapp_outreach_tools.py` | `send_whatsapp_to_contact`, `get_contact_session_messages`, `finish_outreach` |
| Voice outreach tools | `services/voice_outreach_tools.py` | `initiate_voice_call` (browser voice-link offer) |
| WhatsApp bridge | `services/whatsapp_bridge_service/` | WebSocket client to the Go bridge; inbound pipeline, dispatch spec assembly, `wake_session`, crash-recovery sweep |
| Context assembler | `services/context_assembler.py` | Prompt layers: participants, person profile, group hint, dream plans, `outreach_prompt` |
| Goal service | `services/goal_service.py` | `create_goal` / `settle_goal` / `fire_wakeup` / `pump_due_wakeups` |
| Goal tools | `services/goal_tools.py` | LLM-facing goal CRUD via effects outbox |
| Wake service | `services/wake_service.py` | Channel-agnostic `wake_conversation` result relay |
| Attention coordinator | `services/attention/` | Tier 0/1/2 gating of when a dispatch runs |
| Dispatch runner | `services/dispatch_runner.py` | Lock → claim → LLM → tap → history → turn/event bookkeeping |
| Effects outbox | `services/effects.py` | Durable, idempotent external sends (`whatsapp_send`, `whatsapp_send_media`) |
| Session routes | `services/session_route_service.py` | Routes map session keys to channels/chats; `metadata` carries active-outreach state |
| Agenda service | `services/session_agenda_service.py` | Trust-tiered WhatsApp system prompts |
| Channel policies | `services/channel_policies.py` | Inbound accept/drop rules, contact seeding |
| Session dispatch gate | `services/session_dispatch_gate.py` | Per-session lock: one dispatch at a time |
| Tap | `services/tap.py` | Second-chance dispatch when the send tool went unused (`BOB_ENABLE_TAP`) |
| Heartbeat tasks | `heartbeat.py` | `WakeupPumpTask` (deadline wakeups), `EffectPumpTask` (send retries) |
| Occupancy | `services/occupancy.py` | Defers non-urgent texts while a call is live; post-call drain via `wake_session` |
