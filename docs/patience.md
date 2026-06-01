# Patience Dispatch System

## Purpose

Cyborg responds to every incoming WhatsApp message immediately by dispatching an LLM call. This creates two problems:

1. **Fragmented messages.** Humans type in multiple short bursts ("hey", "can you", "actually never mind", "do X instead"). Each fragment triggers a separate dispatch, wasting LLM calls and producing disjointed replies.
2. **Overlapping conversations.** In group chats, multiple participants may type simultaneously. Responding to each message individually produces out-of-order or redundant replies.

The patience system introduces a per-session buffer between message storage and LLM dispatch. Instead of firing immediately, incoming messages accumulate while a fast LLM evaluates whether the sender appears finished. When the conversation settles, the buffered messages are dispatched together as a single coherent LLM call.

## Architecture

```
                          WhatsApp Bridge (Go)
                                  |
                          ChatPresence events
                          IncomingMessage events
                                  |
                                  v
                 +--------------------------------+
                 |  WhatsAppBridgeService (Python) |
                 |                                |
                 |  1. Store message in DB        |
                 |  2. Check patience enabled?    |
                 |         |            |          |
                 |        yes           no         |
                 |         |            |          |
                 |         v            v          |
                 |  submit_to_patience  asyncio.create_task
                 |         |            (_run_dispatch)
                 |         v                        |
                 |  +------------------+            |
                 |  | PatienceBuffer   |            |
                 |  |  (per session)   |            |
                 |  |                  |            |
                 |  | - items[]        |            |
                 |  | - timer_handle   |            |
                 |  | - last_activity  |            |
                 |  +------------------+            |
                 |         |                        |
                 |         v                        |
                 |  +------------------+            |
                 |  | Patience Gate    |            |
                 |  |  (_evaluate_     |            |
                 |  |   urgency)       |            |
                 |  |                  |            |
                 |  | Fast LLM call    |            |
                 |  | (gpt-5.4-mini)  |            |
                 |  +------------------+            |
                 |     |              |             |
                 |  respond_now     wait            |
                 |     |              |             |
                 |     v              v             |
                 |  1s delay     5s silence window  |
                 |     |              |             |
                 |     +-----+--------+             |
                 |           |                      |
                 |           v                      |
                 |     _run_dispatch()              |
                 |     (LLM call + reply)           |
                 +--------------------------------+

  Typing indicator flow:

    Go whatsmeow Client
         |  events.ChatPresence (composing)
         v
    Bridge forwards as whatsapp.chat_presence
         |
         v
    _handle_chat_presence()
         |
         v
    Add PendingItem(item_type="typing") to buffer
    Reset silence timer to 5s
```

## How It Works

### Message Flow

1. An incoming WhatsApp message arrives via the Go bridge WebSocket.
2. `WhatsAppBridgeService._handle_incoming_message` stores the message in the database with `dispatched=0`.
3. If patience is globally enabled (`CYBORG_PATIENCE_ENABLED=true`) and per-session enabled (`{"patience_enabled": true}` in the session route metadata), the message is wrapped in a `PendingItem` and submitted to `submit_to_patience()`.
4. If patience is not enabled, `asyncio.create_task(_run_dispatch())` fires immediately, bypassing the buffer entirely.

### Urgency Evaluation

When a `PendingItem` is submitted, `submit_to_patience` in `patience_gate.py` performs the following:

1. Adds the item to the per-session `PatienceBuffer`.
2. Cancels any existing timer for this session.
3. Checks the safety cap: if `len(buffer.items) >= max_pending_items`, forces immediate dispatch.
4. Calls `_evaluate_urgency()` which sends a fast LLM request to `gpt-5.4-mini` with a concise context summary and the `PATIENCE_SYSTEM_PROMPT`.
5. The LLM returns a JSON decision: `{"decision": "respond_now" | "wait", "confidence": 0.0-1.0}`.
6. Based on the decision:
   - **`respond_now`**: Schedule dispatch after `quick_delay_seconds` (default 1s).
   - **`wait`**: Schedule dispatch after `silence_timeout_seconds` (default 5s).
7. If a new message or typing indicator arrives before the timer fires, the timer is reset.

### Context Provided to the Patience LLM

`_build_patience_context()` assembles a short text summary from three sources:

- **Recent conversation**: The last `max_context_messages` (default 10) dispatched messages from `session_messages`, truncated to 200 chars each.
- **Pending unprocessed messages**: All buffered items with `item_type == "message"`, up to the last 10.
- **Active typing**: Names of unique senders with buffered `item_type == "typing"` items.

The summary ends with the prompt "Should the assistant respond now or wait?".

### Urgency Decision Factors

The `PATIENCE_SYSTEM_PROMPT` instructs the LLM to consider:

- Explicit questions, direct requests, or @mentions -> `respond_now`
- Short fragments, mid-thought, trailing "..." or incomplete sentences -> `wait`
- Multiple users actively chatting (typing indicators or rapid messages from different senders) -> `wait`
- Complete, self-contained statements -> `respond_now`
- Uncertain -> default to `wait`

### WhatsApp Typing/Presence Integration

The Go bridge (whatsmeow client) detects `events.ChatPresence` events when a user is composing. When the state is `ChatPresenceComposing`:

1. The Go client emits a `ChatPresenceEvent` containing `ChatJID`, `SenderJID`, `Media`, and `Timestamp`.
2. The bridge forwards this to the Python service as a `whatsapp.chat_presence` envelope.
3. `WhatsAppBridgeService._handle_chat_presence()` checks if patience is enabled for the session.
4. If enabled, it creates a `PendingItem(item_type="typing")`, adds it to the buffer, cancels the existing timer, and starts a new silence timer set to `silence_timeout_seconds`.

This means that as long as someone in the chat is typing, the dispatch is delayed. The timer only fires when there is a full silence window with no messages and no typing indicators.

Presence subscription is triggered automatically: when patience is enabled for a session, `subscribe_presence()` sends a `subscribe_presence` command to the Go bridge, which calls `whatsmeow.Client.SubscribePresence()` to register for typing notifications for that chat.

## Per-Session Enablement

Patience is controlled per session via the `session_routes.metadata` JSON field:

```json
{"patience_enabled": true}
```

Two conditions must both be true for patience to activate on any message:

1. **Global**: `CYBORG_PATIENCE_ENABLED=true` (the `PatienceSettings.enabled` field).
2. **Per-session**: The session route's metadata JSON contains `"patience_enabled": true`.

If either is false, the message is dispatched immediately via `asyncio.create_task(_run_dispatch())`.

## Slash Commands

### /patience on|off

Available only to trusted contacts. When a message starts with `/`:

- If the sender is a trusted contact, `_handle_slash_command` is invoked.
- If the sender is not trusted, the message is silently dropped (never stored or dispatched).
- `/patience on` sets `patience_enabled: true` in the session route metadata.
- `/patience off` sets `patience_enabled: false`.
- An acknowledgment message is sent back (e.g., "Patience enabled -- waiting for silence before responding").

Any `/`-prefixed message is intercepted before storage and dispatch, regardless of trust level.

## Global Kill Switch

`CYBORG_PATIENCE_ENABLED` environment variable (default: `false`). When set to `false`, the patience system is completely bypassed and all messages are dispatched immediately. This is read once at startup into `Settings.patience.enabled`.

## Configuration

All patience settings are configured via environment variables, loaded in `Settings.from_env()` into a `PatienceSettings` dataclass.

| Environment Variable | Default | Description |
|---|---|---|
| `CYBORG_PATIENCE_ENABLED` | `false` | Global enable/disable. Must be `true` for any patience behavior. |
| `CYBORG_PATIENCE_MODEL` | `gpt-5.4-mini` | LLM model used for urgency evaluation. Should be fast and cheap. |
| `CYBORG_PATIENCE_SILENCE_SECONDS` | `5` | Seconds of silence before dispatching when the LLM decides to wait. |
| `CYBORG_PATIENCE_QUICK_DELAY_SECONDS` | `1` | Seconds to delay when the LLM decides to respond now. |
| `CYBORG_PATIENCE_MAX_PENDING` | `20` | Maximum items in a per-session buffer before forcing immediate dispatch. |
| `CYBORG_PATIENCE_MAX_CONTEXT` | `10` | Maximum recent messages included in the urgency evaluation context. |

## Safety and Failure Handling

- **Buffer cap**: If a buffer accumulates `max_pending_items` (default 20), dispatch is forced immediately regardless of the LLM decision. This prevents unbounded accumulation.
- **LLM failure**: If the urgency evaluation LLM call fails (network error, timeout, invalid JSON response), the system defaults to `respond_now`, ensuring messages are never lost.
- **JSON parsing fallback**: If the LLM response cannot be parsed as JSON, the system checks if the string contains "respond_now". If not found, it defaults to `wait`.
- **In-memory only**: `PatienceBufferRegistry._buffers` is a plain Python dict. All buffered state is lost on process restart. This is acceptable because messages are already persisted to the database with `dispatched=0` before entering the patience system. On restart, the normal dispatch flow will pick them up.

## Key Files

| File | Purpose |
|---|---|
| `packages/cyborg-server/cyborg_server/services/patience_buffer.py` | `PendingItem`, `PatienceBuffer`, `PatienceBufferRegistry` -- per-session in-memory buffers |
| `packages/cyborg-server/cyborg_server/services/patience_gate.py` | `submit_to_patience()`, `_evaluate_urgency()`, `PATIENCE_SYSTEM_PROMPT` -- the decision engine |
| `packages/cyborg-server/cyborg_server/config.py` | `PatienceSettings` dataclass and env var loading in `Settings.from_env()` |
| `packages/cyborg-server/cyborg_server/services/whatsapp_bridge_service.py` | Integration point: patience check, slash commands, typing indicator handling |
| `services/whatsappbridge/internal/whatsapp/client.go` | whatsmeow `ChatPresence` event handling, presence subscription |
| `services/whatsappbridge/internal/bridge/bridge.go` | Forwards `ChatPresenceEvent` as `whatsapp.chat_presence` envelope |
| `services/whatsappbridge/internal/wsproto/protocol.go` | `ChatPresencePayload` type and `TypeChatPresence` constant |
