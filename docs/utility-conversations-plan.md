# Utility Conversations + Self-Serve Sensation Routing — Design Plan

**Status:** proposed (2026-09-09). Origin: Mike's ask in the Bob Security Guard
chat — "play some music when someone turns up" — taken as the first client of a
general capability, not a one-off reflex. Extends
[stimulus-spine-plan.md](stimulus-spine-plan.md); read Part 1 of that first.

Today the spine does `source → stimulus_routes → steer into a conversation`,
and both live routes target human chat groups (cryptobro, frigate→guard).
This plan removes the two remaining dev-task bottlenecks:

1. a route target must be a WhatsApp conversation, and
2. only a developer with SQL access can create a route.

After this: **Bob requests a behavior (utility conversation + route + valves),
Mike approves once in DM, it runs headless.** "When X happens, do Y" costs one
approval message instead of a dev task.

Guiding decisions (with reasons, so they don't get relitigated):

| decision | why |
|---|---|
| targets are headless **utility conversations**, not new chat groups | herald-class behaviors need no human channel; keeps the guard group's triage prompt untouched; no WhatsApp group spray |
| utility conversations are DB rows (`utility_conversations`), not workspace files | spine decision "not workspace files" — platform side of the sandbox boundary; dashboard-joinable, live edits, no restart |
| creation/widening is **owner-approved**, firing is autonomous | same split as steering: steer=human, group-send=autonomous. The approval IS the safety review |
| valves (`hours`, `cooldown_s`, `budget_per_hour`) live on routes, enforced by the router | self-serve behaviors can't rely on editing source daemons (frigate valves are dev-side); hard backstop independent of what a charter says |
| charters are soft scope in v1; hard per-session tool allowlists are later | `make_workspace_tools(ctx, session_key)` is the seam, but least-privilege plumbing is its own project; approval + audit + charter is the proven pattern |
| default model tier = `cheap` alias | frigate sees ~90 person events/day; a utility turn must never burn main-model budget (valves bound it, cheap makes it negligible) |
| steer mode only in v1; `reflex` mode (no LLM) is a later column | herald tolerates tens of seconds; reflex needs an allowlist design and deserves its own review |

---

## Part 1 — Utility conversations

### Shape

- Session key: `agent:<slug>:utility` (e.g. `agent:arrival-herald:utility`).
  Extend `_channel_of` (wake_service + conversations repo) to return
  `"utility"` and `_kind_of` to return `"utility"` for these keys. Everything
  downstream already works: no binding row ever exists, so `wake_conversation`
  takes the `_generic_wake_dispatch` path — a full turn with workspace + goal
  + approval tools, output stored to history. That path stops being a
  "fallback" and becomes a feature.
- Woken only by steers (this plan) and wakeups; never by chat messages.
  Invisible to the chats list; shown under ops (see Dashboard).

### Table (migration `003_utility_conversations.sql`)

```sql
CREATE TABLE IF NOT EXISTS utility_conversations (
  session_key  TEXT PRIMARY KEY,          -- agent:<slug>:utility
  title        TEXT NOT NULL,
  charter      TEXT NOT NULL,             -- behaviour spec, injected per turn
  model_alias  TEXT NOT NULL DEFAULT 'cheap',
  enabled      INTEGER NOT NULL DEFAULT 1,
  created_by   TEXT NOT NULL,             -- requesting session
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
```

### Charter injection + model

- `build_chat_messages` (or the dispatch path) reads the row for utility
  sessions and prepends the charter as a labelled system section
  (`[Charter: arrival-herald]`), below the workspace prompt, above history.
  Charters persist across compaction because they're re-injected per turn —
  the lesson every per-conversation prompt relearns.
- Dispatch resolves the model via the existing alias registry using
  `model_alias` for utility sessions (default `cheap`). Mike can bump one to
  main at approval time if a behavior earns it.

### Reporting (v1.5 — the one piece that touches the send gate)

Utility turns reply in-session; their history is the log. A charter may name a
`report_to` conversation (e.g. the guard group) for one-line firing reports.
That send is cross-conversation, so it would trip the group-send approval gate
per firing — instead, `report_to` is recorded on the **route** and approved
once with the behavior; the send gate gains a pair-scoped exemption
(`utility session → its route's report_to`, one line per firing). If this
slips, v1 ships audit-only: fires are visible in `stimulus_events` +
conversation history, nothing is sent.

---

## Part 1.5 — Subscribing to a specific person (identity in the event stream)

Routes match source/type-glob/level only — by design, the router stays dumb.
So "just Jamie's face" is not a router feature; it's an enrichment at the
source (watchd), which already caches a snapshot per person event and has the
local faces gallery:

- After emitting the generic `activity.person` envelope (never delayed by
  this), watchd runs `whois` on the snapshot — async, best-effort,
  timeout-and-skip on failure. On a confident match it posts a **second**
  envelope: `type = activity.person.<slug>` (gallery-name slug),
  `dedup_key = frigate:<id>:who:<slug>`, `who` + score in the body.
- Dual-emit is what makes it work with first-match routing: the guard's
  `activity.*` route still sees every sighting; a person-specific utility
  adds `type_pattern = 'activity.person.<slug>'` and sees only that person.
  No router changes, no fan-out semantics.
- Caveat carried into every person-specific charter: recognition is
  best-effort (angle, night grain, back-of-head → no match → generic type
  only). Person-specific behaviors have false negatives by construction;
  charters should say what a miss costs (usually: nothing, the moment
  passed). `whois_video` on the clip recovers some misses, seconds later.

Rejected alternative: body-predicate filters on routes (`who = jamie`) —
more general but new matching machinery and unreadable approval DMs; revisit
when a second attribute-filtering need appears. Stopgap if a person-specific
behavior is wanted before watchd enrichment ships: charter-level filter
(route all `activity.person`, charter whois-and-decline) — costs a turn per
sighting, so not the end-state.

---

## Part 2 — Route valves (router-side)

### Migration (same 003 file)

```sql
ALTER TABLE stimulus_routes ADD COLUMN hours TEXT;            -- 'HH:MM-HH:MM' local, NULL = 24h
ALTER TABLE stimulus_routes ADD COLUMN cooldown_s INTEGER;    -- per route
ALTER TABLE stimulus_routes ADD COLUMN budget_per_hour INTEGER;
ALTER TABLE stimulus_events ADD COLUMN route_id INTEGER;      -- stamped by the router
```

`route_id` on events makes valves and audits one join: last fire time and
fires-per-hour are `SELECT`s over `stimulus_events WHERE route_id = ? AND
delivered_steer LIKE 'steer:%'` (30-day prune ≫ any 1h window; no new state).

### Router changes (`stimulus_router.drain`)

After `match_route`, before batching, a route may throttle:

- outside `hours`, inside `cooldown_s`, or over `budget_per_hour` → mark the
  event `processed_at` with outcome `'throttled'`. **Not retried** — a stale
  herald is a wrong herald; throttling means "deliberately dropped, on
  record".
- Existing routes (cryptobro, frigate→guard) get NULL valves: behavior
  byte-identical, valves purely opt-in. Their throttling stays source-side
  (watchd config) as today.

Default valves for self-serve routes are set by the request tool (Part 3),
not by the requester: `cooldown_s ≥ 600`, `budget_per_hour ≤ 6`. Tightening is
free; loosening is widening.

---

## Part 3 — Self-serve creation (`request_sensation_route`)

An LLM tool on the workspace toolset, callable from trusted sessions (Mike's
DM, the guard group). Parameters: `name`, `source`, `type_pattern`, `level`,
`charter`, plus optional `report_to`; valves are set to defaults, tweakable
within the caps above.

Flow:

1. **Write inert**: insert `utility_conversations` row + a `stimulus_routes`
   row with `enabled = 0`, `created_by` = requesting session. Nothing can
   fire; the spec now exists to approve.
2. **Approval**: post an `approval_request` effect (existing
   `approval_tools` machinery, kind `sensation_route`) to Mike's DM rendering
   the full spec — charter verbatim, route pattern, valves, report_to.
3. **Approve** (Mike, `respond_approval`) → the registered `on_approved`
   executor flips `enabled = 1`. **Deny** → rows deleted, requester told.
4. **Widening re-approves**: any change to `source`/`type_pattern`/`level`/
   charter action-scope/`report_to`, or loosening a valve, goes through steps
   1–3 again (the DM shows the diff). Narrowing (tighter valves, charter
   wording that reduces scope) and `enabled = 0` are autonomous — turning a
   behavior off never needs permission.

Kill switches, outermost first: route `enabled`, conversation `enabled`,
`BOB_UTILITY_CONVERSATIONS=off` master switch (house convention).

The tool also serves edits (`route_id` param) and a `list` form — Bob
narrating his own behaviors should be a read, not a DB dive.

---

## Part 4 — Reflex mode (deferred, sketched)

`stimulus_routes.mode`: `'steer'` (today — LLM turn) | `'reflex'` — router
executes a whitelisted command template directly (no LLM, sub-tick latency),
logged identically. Reflex rows carry a `command_template` validated against a
per-skill allowlist (e.g. `sonos.py radio|play-url|status` only). This
subsumes the "herald as watchd config block" idea: it becomes just a route
with mode=reflex, created through the same approved, valve-gated, killable
object. Only worth building when a behavior proves the LLM turn is too slow
or too flaky for it — the herald is not that (yet).

---

## Part 5 — First client: `arrival-herald`

The guard-chat request, as it looks under this plan:

```
route:    frigate / activity.person / action → agent:arrival-herald:utility
charter:  Person event at doorbell or driveway → check what Sonos is doing
          (status --json); if den AND portable are idle, put Bob FM on both
          (radio --room den, --room portable). Never living-room (TV audio).
          Never raise volume; the CLI cap (45) stands. If either room is
          already playing, do nothing. One line to the guard group [v1.5].
valves:   hours 09:00-21:00, cooldown_s 1800, budget_per_hour 4
model:    cheap
```

Doc edits that ship with it (both loaded every turn, so they must agree with
the charter):

- `skills/sonos/skill.md` — "Never start audio autonomously" gains: *except
  utility-conversation charters approved by Mike (see
  stimulus_routes.note); pausing/adjusting was never restricted*. Without
  this, a future Bob reading the skill calls its own house music a violation
  (no-phantom-laws convention).
- `skills/frigate/skill.md` — feed section gains one line: herald-class
  sensations may be routed to utility conversations; the guard turn keeps its
  triage role and may stop/adjust music it finds playing.

Note the herald rides existing source valves too — watchd still applies its
own cooldown/budget/digest before an event ever reaches the spine. Layers
compose; they don't conflict.

---

## Part 6 — Rollout (careful-rollout convention)

1. **Inert.** Migration + router valves + utility dispatch + tool, no routes.
   Unit tests green; verify cryptobro/frigate routes behave identically
   (NULL valves, no route_id regressions).
2. **Herald in observe mode.** Route live, but charter v0 is observe-only:
   log what it would do, touch nothing. Watch a real sample: fire rate vs
   valves, cheap-model turn quality on terse frigate summaries, skip logic.
3. **Charter v1 — act.** The observe→act charter edit goes through the
   widening approval (which dogfoods Part 3 end-to-end). Watch first fires:
   sonos read-back truthfulness, no living-room touches, already-playing
   skips.
4. **report_to** (v1.5) and **reflex mode** (Part 4) only if wanted.

Probe-matrix before the phase-3 flip (eval cases): steer → act with read-back;
steer → room already playing → skip silently; steer → no rooms reachable →
honest failure line, no invented success; charter action must not be refused
(no-phantom-laws). Kill-switch drill: `enabled=0` mid-behavior.

**Tests:** router valve units (hours/cooldown/budget → 'throttled', route_id
stamped, NULL-valve routes untouched); utility dispatch (charter injected,
model alias honored, no binding side-effects); request tool (inert-until-
approved, deny deletes, widening re-approves, narrowing doesn't); migration
seeds/columns; the phase-2/3 eval matrix.

**Non-goals (v1):** dashboard CRUD for routes/conversations (spine plan
already defers it; the join-ability is why they're in the DB), hard
per-session tool allowlists, any change to watchd.

---

## Implementation deltas (2026-09-10 — shipped as migration 003 + services)

The build landed with four deltas from the text above, all discovered
against real code:

- **Fan-out, not first-match.** The plan's own first client collides with
  first-match-wins: `frigate/activity.person` (herald) would be swallowed
  by the guard's `frigate/activity.*`. The router now delivers via EVERY
  enabled matching route (one delivery per target per event, batched per
  tick). The seeded routes are disjoint, so their behavior is unchanged.
  `match_route` (first match) remains for compatibility; `drain` uses
  `match_routes`.
- **Valve state reads a fire log, not events.** With fan-out one event can
  deliver on several routes, so `stimulus_events.route_id` (kept for
  audit) can't feed cooldown/budget. `stimulus_route_fires(route_id, ts)`
  records one row per delivering route per steer; `fire_stats()` reads it.
  Budget counts steers (turns), not events.
- **Approvals CHECK rebuild.** The approvals table allowlists
  `approval_type` in a CHECK constraint; learning `sensation_route`
  required the SQLite rebuild dance (new table + copy + drop view + drop +
  rename + recreate indexes/view) inside migration 003.
- **Narrowing is strict.** Machine-judgeable narrowing = valve tightening
  only, on an ENABLED route (cooldown up, budget down, adding hours where
  none existed). Charter wording, pattern/level changes, hours changes or
  removal, and any edit to a disabled route (including re-enabling one)
  all re-approve — the behavior pauses until approved, deliberately.

Also as shipped: hours are house-local (`BOB_TZ`, default Australia/Perth);
unparseable hours read as 24h with a warning (the tool validates — this
covers manual SQL edits); dead utility targets (kill switch off, row
missing or disabled) are log-only at the router, and the wake path refuses
to dispatch them (second gate). Files: `server/schemas/003_*.sql`,
`repositories/{stimulus,utility_conversations,conversations}.py`,
`services/{stimulus_router,utility_conversations,wake_service}.py`,
`services/{approval_tools,workspace_tools}.py`, `server/config.py`, tests
in `tests/test_utility_conversations.py`.
