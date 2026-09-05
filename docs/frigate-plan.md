# Frigate Skill — Camera Awareness for Bob

**Status:** built + LIVE 2026-09-05 (action flip 13:42 per mike, ahead of
the planned shadow day; first steer verified end-to-end). Companion to
`docs/stimulus-spine-plan.md` — Frigate is stimulus source #2.

Bob knows about human activity around the house (local Frigate 0.14.1 NVR,
five cameras: back, doorbell, driveway, garage, laundry) and can decide to
watch clips and describe what's happening. Two deployables, one contract:

- **Part A — server (repo):** native video injection — `read_video` +
  `VideoInjection`, `input_video` parts with first-frame degradation.
- **Part B — skill (workspace):** `skills/frigate/` — GET-only CLI, feed
  daemon (`bg-frigate-watchd`), offline test suite.

## Decisions with reasons (so they don't get relitigated)

| decision | why |
|---|---|
| REST polling of `/api/events` (30s), not MQTT/webhook | cryptobro pattern; sub-second latency is worthless behind a 60s spine drain + 30–90s LLM turn; no broker creds, no payload shim; the cursor resumes cleanly after crashes |
| **video-first watching** — `read_video` sends the actual clip | GLM-5.3-Flash natively watches video: verified live 2026-09-05 through Bob's OpenRouter key (`input_video` parts; tracked a per-second counter exactly 0→7 and 0→441; ~250–1000 video tok/s scaling with res×fps; 30s@720p15 ≈ 30k tok ≈ $0.005). Contact-sheet+keyframes (`watch --stills`) is the fallback, not the design |
| capability flag `video_input:` in models.yaml, first-frame degradation at the injection site | models rotate (`/model`); only some accept `input_video`; a modal mismatch must never crash a turn |
| steer summary carries the snapshot path | `render_instruction()` renders **summary only** — body never reaches the woken turn, so the summary must be self-sufficient for one-call triage |
| emit at lifecycle milestones (action at first qualifying sighting, info at close), not at "new event" | Frigate events appear open and mutate (`end_time`, `has_clip`); one event → ≤2 envelopes with distinct dedup keys |
| persistent sqlite outbox, not an in-memory deque | Frigate events don't regenerate after retention purges — a daemon crash mid-outage must not drop alerts (cryptobro's deque is fine there because signals regenerate from the TSDB) |
| snapshots cached at emit time, path in the envelope | retention purges silently; the cache is the hedge that keeps `watch`/triage working days later |
| skill-local `state.db`, never bob.db | sandbox boundary (cryptobro convention) |
| all-wake policy (mike 2026-09-05) with cooldown+budget valves | NVR sees ~90 person events/day; per-camera 10-min cooldown + 6/hour budget are the real gates — tunable in config.toml, no code |
| GET-only, zero exceptions | read-only discipline (redbark pattern, minus even the one elevated verb) |
| transient unit `bg-frigate-watchd`, accepted reboot gap | radio convention; gap-free escalation = the persistent 5-line user unit below (Mike installs — the bash sandbox can't write `~/.config/systemd/user/`) |
| camera imagery → serving-model vision path is an external API | same as every WhatsApp photo; recorded as accepted (mike, 2026-09-05) |

## Part A — server-side video injection

- `server/services/tools.py`: `VideoInjection(text, data_url, path="")`.
- `server/services/openai_service.py`: `_tool_result_messages()` — one
  helper for all tool-result appends at both call sites
  (`chat_with_tools`, `chat_stream_with_tools`); this also fixed the
  streaming path, which previously had **no** ImageInjection handling at
  all (read_image tool results on streamed turns were silently stringified).
- `server/services/workspace_tools.py`: `read_video` (workspace-only,
  mp4/m4v/mov/webm/mpeg, ≤20MB, one per tool set ≈ per turn).
- `server/services/llm_dispatch.py`: trace capping recognizes input_video.
- `config/models.yaml`: `video_input: {z-ai/glm-5.3-flash: true}`.
- Tests: `tests/test_video_injection.py` (part shapes, degradation,
  guards, registry flag).

Follow-on (separate rollout, needs its own media-cap policy): inbound
WhatsApp video currently inlines first-frame only; `read_video` now makes
true video understanding possible there.

## Part B — the skill

```
skills/frigate/
  skill.md            trigger + safety + triage/watch recipes
  frigate.py          GET-only CLI: cameras|events[--since --until]|event|snapshot|live|
                      watch[ --stills]|note|recap --since --until|status
  watchd.py           feed daemon: poll → upsert → classify → cache → outbox → poster
  config.toml         all tuning (re-read every cycle — live edits, no restart)
  state.db            events + persistent outbox + cursor/cooldown/budget state
  cache/{snapshots,clips,frames}/   pruned: snaps 7d, clips/frames 2d
  run/health.json     rewritten every cycle
  tests/test_frigate.py  offline mock-NVR + mock-spine suite (15 cases)
```

`watch <id>`: fetch clip → transcode `scale=min(854,iw), fps=5, -t 120,
crf 30` (~1.3MB per 36s clip ≈ 4–10k video tokens ≈ fractions of a cent) →
print the `read_video` line + snapshot path + optional faces whois hint.
`--stills` produces a 3×3 contact sheet + 2 keyframes instead.

Failure modes: Frigate unreachable → backoff (cap 600s) + one
`health.stale` action envelope + hourly info nudges + recovery record;
bob.service down → outbox holds and retries (spine dedup makes re-delivery
impossible); spine 4xx → drop (a rejected envelope can't be retried into
acceptance); clip 404 → `CLIP_GONE` + snapshot fallback; reboot → daemon
down until restarted (status flags it; Bob's skill.md teaches restart).

## Rollout (careful-rollouts convention)

| phase | state | verify |
|---|---|---|
| 0 — server injection | **done 2026-09-05** | 820 repo tests green; restart; live sanity turn (read_video described a test clip, counter 0→7) |
| 1 — inert skill | **done 2026-09-05** | CLI live against the NVR; 15 offline tests; real CCTV watch drill (36s backyard clip, correct person/motion/timeline description, camera model off the OSD) |
| 2 — shadow feed | **live 2026-09-05 11:00–13:25** | ~20 envelopes sampled: lifecycle pairs, snap paths cached, dedup across restarts, UTC-summary bug found+fixed |
| 3 — action flip | **LIVE 2026-09-05 13:42 (mike, ahead of the shadow day)** | first steer: doorbell 13:42 → `steer:ok` → Bob triaged and reported to the DM in-turn |

Route (inserted disabled; flip = `UPDATE stimulus_routes SET enabled=1
WHERE source='frigate'`):

```sql
INSERT INTO stimulus_routes(source, type_pattern, level, target_session, enabled, priority, note, created_at, created_by)
VALUES ('frigate','activity.*','action','agent:main:whatsapp:dm:61456224867', 0, 0,
        'Frigate person activity -> Mike DM', '2026-09-05', 'bob');
```

Kill switches (independent): `config.toml [feed] enabled=false` (source) ·
route `enabled=0` (spine) · `systemctl --user stop bg-frigate-watchd`.

Unit (Bob's process tools own it):

```
set -a; . /home/bob/config/.env; set +a; cd /home/bob/workspace; \
  exec python3 skills/frigate/watchd.py >> .bg/logs/frigate-watchd.log 2>&1
```

Gap-free escalation (Mike, one time):

```
~/.config/systemd/user/bg-frigate-watchd.service
  [Unit] Description=frigate feed daemon
  [Service] ExecStart=/bin/bash -c '<command above>'
  Restart=on-failure
  [Install] WantedBy=default.target
  → systemctl --user enable --now bg-frigate-watchd
```

## Interval recall + transcripts (2026-09-05, same day)

`recap --since 15:00 --until 16:00` answers "what happened 3–4pm": every
event in the window (times local to the house — naive ISO used to parse as
UTC, fixed), merged with **stored viewing notes**. `note <id> <text>` is
the durable transcript: chat history compacts, the note column in state.db
doesn't (guarded-ALTER migration, applied in place). Bob is taught to note
after every watch and to answer interval questions from notes first,
re-watching only unclear events — a 150-event back-camera afternoon is not
worth re-watching.

## Watch points

- **Wake rate under all-wake**: ~90 person events/day measured; if the
  budget cap (6/hour) is saturating most hours, tighten `cooldown_min` or
  add `action_zones` (NVR has none configured yet — adding them in Frigate
  is free and makes rules meaningful).
- **Night footage quality**: unverified on GLM; phase-2 gate before flip.
- **Token cost of watches**: ~$0.001–0.005 each — fine unless watches
  become habitual.
- Docker instances: skill seeds from the repo bundle; `config.toml` ships
  `enabled=false` so fresh instances start inert.

## History

- 2026-09-05: built (phases 0–1), shadow feed + disabled route live;
  video-injection landed server-side with the streaming-image fix.
