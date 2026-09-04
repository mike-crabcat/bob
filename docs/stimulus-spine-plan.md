# Stimulus Spine + Cryptobro Server — Design Plan

**Status:** proposed (2026-09-04). Settled across design review with Mike.

Bob is meant to evolve toward many external stimulus feeds — sources raising
events that get routed into conversations. This plan defines the platform
spine that makes that a ~10-line job per source, and applies it to the first
source: the cryptobro skill upgrade from CLI-only to server+CLI (radio
station pattern).

Two deployables, one contract:

- **Part 1 — Spine** (repo, `bob.service`): ingest endpoint + `stimulus_events`
  + `stimulus_routes` + heartbeat router → steer.
- **Part 2 — Cryptobro server** (workspace skill, `bg-cryptobro.service`):
  Swyftx poller, TSDB, signal engine, read-only proxy; CLI becomes a client.
- **Part 3 — Rollout** in four phases, spine shipping inert.

Guiding decisions already made (with reasons, so they don't get relitigated):

| decision | why |
|---|---|
| ingest via `POST /api/v1/stimulus/events`, not direct DB writes | SQL-ownership rule stays intact (no external INSERTs past `repositories/`); bob.service remains sole DB writer; schema fully platform-owned |
| events + routes live in bob.db | matches platform habit (wakeups table owns schedules); live edits, no restart, no mtime machinery; debug story is a join; dashboard-editable |
| delivery is a **steer** into the target session | reuses live steering machinery; provenance markers; in-session replies don't trip the cross-group approval gate (that gate is for proactive sends to *other* conversations) |
| **not** webhooks/HMAC machinery | too much contract for local bg units; a dumb POST route + dedicated token is enough |
| **not** workspace files | workspace is Bob's domain — he restructures it; `~/data`/DB is the platform side of the sandbox boundary |
| router is a 60s heartbeat task, no self-throttle | detection resolution is 30s (quote poll); Bob's own turn (30–90s) dominates latency; batch-per-tick so correlated alerts make one turn |
| server never trades (v1) | tier-2 contract: Bob decides within the band; caps + rationale + ledger audit stay in the CLI path |

---

## Part 1 — The stimulus spine

### Ingest endpoint

```
POST /api/v1/stimulus/events          (localhost-bound like the rest of the API)
Authorization: Bearer $BOB_STIMULUS_TOKEN

{ "source": "cryptobro", "type": "signal.momentum", "level": "action",
  "dedup_key": "mom1h:SOL:2026-09-04", "ttl_s": 600,
  "target_hint": "crypto", "summary": "SOL +3.2% over 1h",
  "body": { "asset": "SOL", "chg_1h": 3.2, "rate": 145.25 } }

→ 201 {"id": N}   |   200 {"id": N, "duplicate": true}   |   4xx on bad envelope
```

- Deliberately dumb in v1: validate envelope → repository INSERT → return.
  No fan-out, no callbacks. The router drains the table.
- Idempotent by `dedup_key` (`INSERT OR IGNORE` semantics) so source retries
  are always safe.
- **Auth**: dedicated `BOB_STIMULUS_TOKEN` in `~/config/.env` (sources get it
  via the unit env-load, radio pattern). Never the dashboard secret — worst-case
  leak of this token can only post stimulus events. Ships with its own bearer
  check regardless of the api-security token-gate branch state.
- Envelope fields are the whole source contract; schema is platform-private.

### Tables (migration in `server/schemas/`)

```sql
CREATE TABLE stimulus_events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              TEXT NOT NULL,
  source          TEXT NOT NULL,
  type            TEXT NOT NULL,
  level           TEXT NOT NULL DEFAULT 'info',   -- info | action
  dedup_key       TEXT,
  ttl_s           INTEGER,
  target_hint     TEXT,
  summary         TEXT NOT NULL DEFAULT '',
  body_json       TEXT NOT NULL DEFAULT '{}',
  processed_at    TEXT,                            -- NULL = pending
  delivered_steer TEXT                             -- audit: what it woke
);
CREATE UNIQUE INDEX idx_stimulus_events_dedup
  ON stimulus_events(dedup_key) WHERE dedup_key IS NOT NULL;

CREATE TABLE stimulus_routes (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  source         TEXT NOT NULL,              -- 'cryptobro' | '*'
  type_pattern   TEXT NOT NULL DEFAULT '*',  -- glob: 'signal.*'
  level          TEXT NOT NULL DEFAULT 'action',
  target_session TEXT,                       -- NULL = log-only
  enabled        INTEGER NOT NULL DEFAULT 1,
  priority       INTEGER NOT NULL DEFAULT 0, -- first enabled match wins
  note           TEXT,
  created_at     TEXT NOT NULL,
  created_by     TEXT
);
```

Migration seeds route #1: `cryptobro / signal.* / action →
agent:main:whatsapp:group:120363410716086644` (note: `"Crypto Bob"`),
resolved from the group display name at seed time.

Retention: router prunes processed rows older than ~30 days (events are not
a ledger; `delivered_steer` keeps the pointer).

### Router — `StimulusRouterTask` (heartbeat, every tick)

1. `SELECT` pending rows (`processed_at IS NULL`), drop stale ones
   (`ts + ttl_s < now` → mark processed, note "expired").
2. Match each row against `stimulus_routes` (first enabled match by
   `(priority, id)`); unrouted → log-only.
3. **Batch per target session**: all due events for one target in one tick
   become ONE steer (correlated signals — a market move trips several assets
   in the same 30s poll — must not spawn concurrent turns; 2026-09-04
   incident).
4. Create the steer via the existing steering service; then mark rows
   `processed_at` + `delivered_steer`. Crash between the two re-delivers;
   dedup absorbs it. Worst case is *late*, never *lost*.
5. `level: info` never steers — logged only.

### The steer (what the woken turn sees)

Provenance `steer` (renders as a system relay, never as a human speaking).
Template carries the audit numbers so the decision doesn't need extra tool
calls (though the turn may still check `plan`/`trend` — grounding rules
apply to any band state it states):

```
[Stimulus: cryptobro signal.momentum, dedup mom1h:SOL-2026-09-04]
SOL +3.2% over 1h — rate 145.25 AUD, 24h vol 4.0B, 60-min high broken.
If you take it: buy via the cryptobro CLI citing these numbers as the
rationale, then report here. If not: no reply is needed.
```

The **silent-decline line is mandatory** — alerts must be declinable via the
existing `NO_REPLY` path or every alert manufactures channel chatter.

Replies are in-session (the turn belongs to the target conversation), so no
cross-group approval is involved.

### Dashboard (ops)

Small `stimulus` section on the existing ops pattern: routes CRUD + recent
events with matched route and delivery state. Post-v1 nicety, not launch
blocker — but the join-ability is why routes live in the DB.

### Future sources (no new machinery)

- HA zone transitions: `LocationFetchTask` diffs zone changes → POSTs
  `zone.enter/leave` events. Source #2 candidate.
- Radio on-air events, face-id enrollments: same door.
- Quiet hours / digesting / shadow mode: router config, never source changes.

---

## Part 2 — Cryptobro server (first consumer)

### Process

`skills/cryptobro/server.py`, transient user unit `bg-cryptobro.service`
(radio pattern: `~/config/.env` env-load, logs → `.bg/logs/cryptobro.log`).

### The one Swyftx client

Token-bucket rate governor honoring `Retry-After` — the 2026-09-04 wedge
lesson, centralized. Poll loops:

- quotes for the watchlist — 30s
- market detail (trend inputs) — 5 min
- balances — 5 min

Watchlist = `strategy.toml` assets ∪ currently held. All responses land in
the TSDB. The CLI keeps today's fast-fail direct mode as **fallback** when
the server is down, so nothing regresses.

### TSDB — `skills/cryptobro/ticks.db`

- `ticks(ts, asset, buy, sell, mid, vol24h)`, index `(asset, ts)`
- 1-min candles derived, kept indefinitely; raw ticks pruned ~14 days
- rate-state (last 429 / retry-after) so the proxy knows throttle status

### Signal engine

Rules in `strategy.toml`:

```toml
[server]
poll_quotes_s = 30
poll_detail_s = 300
retention_days = 14

[signals]
momentum_1h_pct = 3.0
momentum_24h_pct = 8.0
breakout_min = 60
cooldown_min = 240        # per (signal, asset)
```

On fire: build the envelope (numbers included, `dedup_key` encodes
signal+asset+day, `ttl_s` ~600, `level: action`) and POST to the stimulus
endpoint with an in-memory retry deque (bob.service down ⇒ hold and retry;
dedup makes it safe). **Never trades.**

### Proxy API (localhost, read-only)

`/price?assets=…`, `/trend`, `/balances` served from the TSDB with staleness
stamped. No order verbs — buys/orders/me stay CLI-direct in v1 (one order
path, unchanged audit chain, server-down ≠ can't trade).

### CLI changes (`cryptobro.py`)

- `price`/`trend`/`balances`: server-first, staleness printed, direct
  fallback (with the existing capped-retry fast-fail).
- New `signals` command: current signal state + cooldowns (for narrating).
- `buy`/`me`/`orders`/`ledger`: unchanged.

### Safety invariants

- Server never places orders (v1). Phase-4 `auto` mode, if ever, reuses the
  `cmd_buy` validation ladder (caps stay in code) — paper first.
- `HALT` respected by the signal engine (no alerts while halted is a
  decision for phase 3 review; buys blocked as today regardless).
- Swyftx key stays `chmod 600` in the skill dir; only the server and
  CLI-fallback read it. The stimulus token reaches the unit via env-load.

---

## Part 3 — Rollout (careful-rollout convention)

1. **Spine inert + server reads.** Tables/endpoint/router shipped with no
   enabled action routes; cryptobro server + TSDB + proxy live; CLI
   dual-path. Watch: poll rate vs 429s, tick data quality on a real sample.
2. **Signals in shadow.** Engine live, posts `level: info` (log-only).
   Review alert quality in the events table/dashboard before any waking.
3. **Wake on.** Flip the seeded route to `action`. Probe-matrix the steer
   template first (eval cases: alert → decline silently; alert → buy with
   cited rationale; alert → stale band state must be checked, not assumed).
   Watch the first real alerts end-to-end.
4. **Optional, later.** `auto` mode for signals that genuinely need
   second-scale response (that's the correct escalation for speed — machine
   execution — not a faster alert path; a minute-long LLM turn is structural).

Tests: router unit tests (match/batch/dedup/TTL/expiry), endpoint auth +
envelope validation, migration seeds, cryptobro offline suite extension
(signal fires → envelope POSTed with retry; cooldown honored), plus the
phase-3 eval matrix.
