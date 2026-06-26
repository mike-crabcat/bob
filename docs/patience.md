# Patience Dispatch System

## Purpose

Bob's patience system sits between incoming WhatsApp messages and the main LLM dispatch. It does two jobs:

1. **Batching.** Instead of firing one LLM call per incoming message, the patience buffer accumulates messages and dispatches them together as a single coherent call. A user typing "hey", "can you", "actually do X" produces one dispatch, not three.

2. **Relevance gating (optional).** When enabled, the patience LLM also decides whether Bob should respond at all. If the conversation is casual banter between other people and Bob isn't addressed, the main LLM is skipped entirely — no reply, no LLM cost.

## Architecture

```
                       WhatsApp Bridge (Go)
                               |
                       IncomingMessage events
                               |
                               v
              +--------------------------------+
              | WhatsAppBridgeService (Python) |
              |                                |
              |  1. Store message in DB        |
              |     (session_messages,         |
              |      dispatched=0)             |
              |                                |
              |  2. submit_to_patience()       |
              |     (always — Phase 2)         |
              |                                |
              |     +---------------------+    |
              |     | PatienceBuffer      |    |
              |     |  (per session)      |    |
              |     |  - items[]          |    |
              |     |  - timer_handle     |    |
              |     |  - last_activity    |    |
              |     |  - last_evaluated_at|    |
              |     |  - last_respond     |    |
              |     +-----------+---------+    |
              |                 |              |
              |     patience_enabled?          |
              |        |           |           |
              |       yes          no          |
              |        |           |           |
              |        v           v           |
              |   _evaluate_    fixed delay    |
              |    urgency()    (settle_sec)   |
              |   (LLM call)                   |
              |        |                       |
              |   respond=true|false           |
              |    (only if relevance          |
              |     gating is on)              |
              |        |       |               |
              |        |   respond=false        |
              |        |       |               |
              |        |       v               |
              |        |   _flush_without_     |
              |        |     dispatch()        |
              |        |   (no main LLM)       |
              |        v                       |
              |   timer fires                  |
              |        |                       |
              |        v                       |
              |   _run_dispatch()              |
              |   (main LLM call + reply)      |
              +--------------------------------+
```

## Two behavior modes

| Mode | Trigger | Buffer? | Patience LLM? | Relevance gate? |
|---|---|---|---|---|
| Patience on | `patience_enabled: true` in route metadata | yes | yes (per batch) | optional (`patience_relevance_gating: true`) |
| Patience off | `patience_enabled: false` or unset | yes | no | no |

Both modes batch. Patience-on adds the LLM gate; patience-off uses a fixed short settle delay (default 1.5s) to absorb bursts.

## Per-session flags (route metadata)

Both flags live in `session_routes.metadata` JSON. Set via slash commands from a trusted contact in the chat.

```json
{
  "patience_enabled": true,
  "patience_relevance_gating": false
}
```

- `patience_enabled` — enables the patience LLM timing gate.
- `patience_relevance_gating` — extends the patience LLM to also decide whether to respond. Only meaningful when `patience_enabled` is also true. The `/relevance on` command warns if patience is off.

## Slash commands

### `/patience on|off`
Toggles `patience_enabled` for the current session.

### `/relevance on|off`
Toggles `patience_relevance_gating` for the current session. Requires patience to be on for the flag to take effect; `/relevance on` warns if `/patience` is currently off.

Both commands are restricted to trusted contacts (same gate as all slash commands).

## How a batch flows through

For each incoming message:

1. **Storage.** Message is persisted to `session_messages` with `dispatched=0`.
2. **Buffer.** Wrapped in a `PendingItem` and added to the per-session `PatienceBuffer`. Existing timer is cancelled.
3. **Dispatch-in-progress check.** If a main LLM dispatch is already running for this session, the new item is buffered silently and the in-progress dispatch will claim it via `mark_dispatched`. No further evaluation.
4. **Safety cap.** If the buffer has `>= max_pending_items` (default 20) messages, behavior splits:
   - No prior relevance decision, or last decision was respond=true → force dispatch immediately.
   - Last decision was respond=false → flush without dispatch (mark messages claimed, no main LLM call).
5. **Gate.**
   - **Patience on**: call `_evaluate_urgency` — the patience LLM (gpt-5.4-mini) returns `{respond, wait_seconds, reason}`.
   - **Patience off**: skip the LLM; use a fixed `settle_seconds` delay (default 1.5s).
6. **Mark batch evaluated.** `buffer.last_evaluated_at = buffer.last_activity` and `buffer.last_respond = effective_respond`. From this point, those items are no longer "pending" for future patience contexts (see Pending semantics below).
7. **Decision branch.**
   - `effective_respond=false` (only possible when relevance gating is on) → call `_flush_without_dispatch` (mark messages dispatched, no timer, no main LLM). Next arriving message re-runs the patience LLM.
   - `effective_respond=true` → set timer for `wait_seconds`. On fire, run `_dispatch_and_cleanup` → `_run_dispatch` (main LLM call).
8. **Re-evaluation during the wait.** If a new message arrives before the timer fires, the timer is cancelled and the gate runs again on the larger batch (today's behavior — explicitly preserved).

### Effective respond flag

When `patience_relevance_gating` is **off**, the LLM's `respond` field is ignored — `effective_respond` is always `true`. This preserves today's behavior exactly: the patience LLM only decides timing, never skipping.

When `patience_relevance_gating` is **on**, `effective_respond = decision.respond` (the LLM's judgment).

## Pending semantics

"Pending" means *newly arrived, not yet evaluated by the patience LLM*. Once the patience LLM runs on a batch, those items are marked evaluated via `buffer.last_evaluated_at = buffer.last_activity`. Future patience contexts (`_build_patience_context`) only include items with `timestamp > last_evaluated_at`.

This matters when a batch is skipped and then a new message arrives: the patience LLM sees only the *new* message (not the previously-skipped batch) in its "pending" section, even though all items are still in the buffer waiting to be claimed by the next dispatch.

Recent dispatched history from `session_messages` (last 10, `dispatched=1`) is also included in the context for conversation continuity — that part is unaffected.

## Safety and failure handling

- **Buffer cap** (`max_pending_items`, default 20): forces dispatch or flush (per step 4 above). Prevents unbounded accumulation.
- **LLM call failure**: defaults to `PatienceDecision(respond=True, wait_seconds=3.0, reason="llm-call-failed")`. Never skip on a fault — failures should not produce silence.
- **Context-build failure**: same fallback.
- **JSON parse failure**: extracts the first number from the response as `wait_seconds`; `respond=True` assumed.
- **In-memory only**: `PatienceBufferRegistry._buffers` is a plain Python dict. Buffered state is lost on restart. Acceptable because messages are persisted with `dispatched=0` first; on restart, the next dispatch will claim them.
- **Memory capture unaffected**: `run_silent_turn_extraction` reads `session_messages` directly and does not check the `dispatched` flag. Even skipped batches are captured for memory.

## Expected log lines

All patience activity is logged at INFO level. Filter with:

```bash
journalctl --user -u bob.service -f | grep -E "patience|relevance"
```

### Normal flow (patience on, respond=true)

```
patience: new message item for agent:main:whatsapp:group:GID from alice, buffer=1 messages + 0 typing
patience LLM decided respond=True wait=8s for agent:main:whatsapp:group:GID (reason: direct question)
patience: timer=8.0s for agent:main:whatsapp:group:GID (reason: direct question)
patience timer fired for agent:main:whatsapp:group:GID, dispatching
patience: buffer cleared for agent:main:whatsapp:group:GID after dispatch
```

### Skip flow (patience on + relevance on, respond=false)

```
patience: new message item for agent:main:whatsapp:group:GID from bob, buffer=1 messages + 0 typing
patience LLM decided respond=False wait=0s for agent:main:whatsapp:group:GID (reason: casual banter, not addressed)
patience: skip for agent:main:whatsapp:group:GID (reason: casual banter, not addressed), 1 messages marked dispatched without main LLM
patience: flushed 1 message(s) for agent:main:whatsapp:group:GID without main LLM (skip)
```

### Burst during the wait (timer reset)

```
patience: new message item for ... from alice, buffer=1 messages + 0 typing
patience LLM decided respond=True wait=10s for ... (reason: complete thought)
patience: timer=10.0s for ... (reason: complete thought)
patience: new message item for ... from alice, buffer=2 messages + 0 typing       # second message
patience: new activity during evaluation for ..., skipping timer                  # if LLM still running
patience LLM decided respond=True wait=5s for ... (reason: still going)
patience: timer=5.0s for ... (reason: still going)
```

### Safety cap — force dispatch (no prior decision)

```
patience: new message item for ... from alice, buffer=20 messages + 0 typing
patience buffer cap hit for ... (20 messages), forcing dispatch
patience: buffer cleared for ... after dispatch
```

### Safety cap — flush after skip (relevance on, last decision was respond=false)

```
patience: new message item for ... from alice, buffer=20 messages + 0 typing
patience buffer cap hit for ... (20 messages) after skip decision, flushing without dispatch
patience: flushed 20 message(s) for ... without main LLM (skip)
```

### Dispatch in progress

```
patience: dispatch in progress for ..., buffering silently
```

## Verification cheatsheet

| Question | Grep pattern | Expected |
|---|---|---|
| Is patience on for session X? | `patience check: session=X` | `enabled=True` in the log line |
| Is relevance gating on for session X? | `patience check: session=X` | `relevance=True` in the log line |
| Did the relevance gate skip a batch? | `patience: skip for` | count of skips per session |
| Did the safety cap force-flush after skips? | `flushing without dispatch` | should be rare; investigate if frequent |
| Are patience LLM calls failing? | `patience: LLM call failed` | should be near-zero; non-zero means degraded |
| Is the main LLM running for skipped batches? | (cross-reference `LLM dispatch tools: ... category=whatsapp_incoming` against skip log lines) | should not appear immediately after a skip for the same session |

### Counting skips vs dispatches per session (last hour)

```bash
journalctl --user -u bob.service --since "1 hour ago" --no-pager \
  | grep -oE "patience: (skip|buffer cleared).*agent:main:whatsapp:group:[0-9]+" \
  | sort | uniq -c
```

A healthy relevance-gated session shows a meaningful skip-to-clear ratio (e.g. 5–20 skips per clear). A ratio of 0:1 means relevance gating isn't doing anything (likely Bob is always being addressed, or the flag is off).

## Configuration

All settings via env vars, loaded into `PatienceSettings` in `config.py`.

| Env var | Default | Description |
|---|---|---|
| `BOB_PATIENCE_ENABLED` | `false` | Loaded into `PatienceSettings.enabled` but **not currently consulted** by the dispatch path. Per-session route metadata is the real gate. |
| `BOB_PATIENCE_MODEL` | `gpt-5.4-mini` | Model used for patience LLM calls. Keep this fast and cheap. |
| `BOB_PATIENCE_MAX_PENDING` | `20` | Max messages in a per-session buffer before safety cap fires. |
| `BOB_PATIENCE_MAX_CONTEXT` | `10` | Max recent dispatched messages included in the patience context. |
| `BOB_PATIENCE_OFF_SETTLE_SECONDS` | `1.5` | Phase 2. Fixed delay used when patience is off (no LLM eval, just burst absorption). |
| `BOB_PATIENCE_BOT_NAME` | `Bot` | Name substituted into the patience system prompt. |

## Key files

| File | Purpose |
|---|---|
| `packages/bob-server/bob_server/services/patience_buffer.py` | `PendingItem`, `PatienceBuffer`, `PatienceBufferRegistry` — per-session in-memory state including `last_evaluated_at` / `last_respond` |
| `packages/bob-server/bob_server/services/patience_gate.py` | `submit_to_patience`, `_evaluate_urgency`, `_patience_system_prompt`, `_flush_without_dispatch`, `PatienceDecision` |
| `packages/bob-server/bob_server/services/whatsapp_bridge_service/_service.py` | Patience check at the dispatch fork (~line 1173); reads route metadata flags |
| `packages/bob-server/bob_server/services/whatsapp_bridge_service/_slash_commands.py` | `/patience` and `/relevance` slash command handlers |
| `packages/bob-server/bob_server/services/session_service.py` | `mark_dispatched` (UPDATE session_messages SET dispatched=1) — used by both `_run_dispatch` and `_flush_without_dispatch` |
| `packages/bob-server/bob_server/config.py` | `PatienceSettings` dataclass + env var wiring |
| `packages/bob-server/tests/services/test_patience_gate.py` | Unit tests covering both modes, skip path, safety cap, pending semantics |
