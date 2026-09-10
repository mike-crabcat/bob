---
name: frigate
description: Read-only window into the local Frigate NVR — query camera activity, fetch snapshots and clips, and actually watch clips with vision (read_video). Includes an always-on person-activity feed that arrives as stimulus steers.
trigger: when Mike asks about cameras, who's at or on the property, what happened outside, deliveries or motion at the house, or when a frigate stimulus steer arrives
---

# Frigate — house cameras

Five cameras (back, doorbell, driveway, garage, laundry) on the local Frigate
NVR. Everything runs through `python3 skills/frigate/frigate.py <command>`
from the workspace root. A background daemon (`bg-frigate-watchd`) tracks
person events and feeds them into the stimulus spine.

## Non-negotiable safety

- **GET-only.** This skill reads the NVR; it never deletes events, toggles
  retention, or changes config. Those are Mike's tasks in the Frigate UI.
- **Camera imagery is household-private.** Describe in text by default.
  Send a snapshot or clip only to Mike's DM or the **Bob Security Guard**
  group (the designated security channel, Mike-only) — only when it clearly
  helps. Never to any other group chat. Never into third-party tools beyond
  the normal vision path.
- Faces: `whois` against the people/ galleries is fine (local-only; names in
  Mike's DM or the Bob Security Guard group only). Never enroll new
  household members without Mike.

## Commands

```
python3 skills/frigate/frigate.py cameras                  # cameras + zones
python3 skills/frigate/frigate.py events --since 2h        # recent events (add --camera doorbell, --limit 50, --json)
python3 skills/frigate/frigate.py event <id>               # full record
python3 skills/frigate/frigate.py snapshot <id>            # cache + print snapshot path
python3 skills/frigate/frigate.py live [camera]            # current frame
python3 skills/frigate/frigate.py watch <id>               # prep a clip for read_video
python3 skills/frigate/frigate.py watch <id> --stills      # contact sheet + keyframes fallback
python3 skills/frigate/frigate.py status                   # daemon + feed health
```

## The feed (stimulus steers)

Since 2026-09-10 person events steer into the **frigate-watch utility
conversation** (`agent:frigate-watch:utility`) — a headless cheap-model turn
that watches each clip and only escalates when concerning:

```
[Stimulus: frigate activity.person, dedup frigate:1788...-da9lzg]
person at doorbell 14:32 (48s, score 0.87) clip=y snap=skills/frigate/cache/snapshots/202609/<id>.jpg
If you act on this, do it with the platform's tools and report here; if not, no reply is needed.
```

**frigate-watch turn recipe:** the dedup key holds the event id after
`frigate:`. `frigate.py watch <id>` preps the clip, then `read_video` it
(snapshot as fallback when there is no clip). Run `whois_video` on the clip
to name who it is whenever the footage allows. **The default is silence** —
call `send_report` (which wakes a guard turn in the Bob Security Guard
group) ONLY for:

- an unknown person (not Mike, not a known household member/visitor)
- genuinely unusual behaviour — someone lingering, trying doors, looking
  in windows, entering the property the wrong way
- unusual hours (late night / very early) where presence itself is notable
- something Mike explicitly asked to be told about

**Never report Mike himself** doing normal things (gardening, pool, bins,
coming home) — he knows where he was. Known people doing routine things
(Helen doing washing, the postie) are also silence. A face-match on Mike or
a known household member is the STRONGEST silence case — a confirmed him is
never news to him, any hour. Exception: impossible contexts (a Mike-match
at 3am, or while he's verifiably elsewhere). Silence means NO send_report —
your reply text is just the watch log in this conversation's history.
Night clips degrade recognition: say what you could not determine rather
than guessing; never fabricate a face match.

**Guard-group turn recipe:** a `[Report from agent:frigate-watch:utility]`
wake means frigate-watch already watched the clip and judged it concerning.
Raise it in the group (who/what/where/when + event id); fetch the snapshot
or clip yourself only if the report leaves ambiguity. The guard turn keeps
its triage role and its other duties (e.g. music for arrivals).

The route carries spine valves (600s cooldown, 6 wakes/hour) on top of
watchd's own upstream throttling — a burst of events collapses to fewer
watches by design.

`health.stale` steers mean the NVR has been unreachable — mention it to
Mike if he hasn't noticed; nothing else to do.

Person events may ALSO be routed (fan-out) to utility conversations —
headless charters Mike approved, such as an arrival herald that plays
music when someone turns up. Those turns are separate from yours: keep
your triage role exactly as written. If you find herald music playing,
you may stop or adjust it — pausing was never restricted, only starting.

## Watching a clip

```
python3 skills/frigate/frigate.py watch <event_id>
```

downloads the clip and transcodes it to ~854px/5fps (a few MB — cheap to
watch). It prints a `read_video <path>` line: run `read_video` on that path
and you see the actual footage — motion, sequence, timeline. Then:

- Optionally name who: prefer `whois_video` on the downloaded clip —
  `skills/faces/.venv/bin/python skills/faces/faces.py whois_video <clip path>`
  (best frontal crop + averaged embedding; more reliable than the snapshot).
  Snapshot `whois` still works when no clip is available (CLIP_GONE).
- Report factually in-session: who/what, what they did, when. Only state
  what you can actually see — night footage is grainy; say so rather than
  inventing detail. If the description matters and frames are ambiguous,
  hand the artifact paths to a `create_subagent(agent_type="claude")` for a
  second look — claude subagents exist only in trusted sessions (Mike's
  DM); in the Bob Security Guard group only script subagents are available,
  so describe what you can see, hedge clearly on the unclear parts, and
  flag it for a DM second look if it matters.
- If `watch` prints `CLIP_GONE`, retention already purged the clip —
  describe what you can from the snapshot.
- If `read_video` reports the model cannot watch video, rerun `watch` with
  `--stills` and `read_image` the sheet + keyframes instead.

Clip downloads and transcodes take seconds, not minutes — plain `bash` is
fine. Only go through a script subagent if you're doing several.

**Always note what you watched** — chat history compacts, notes persist:

```
python3 skills/frigate/frigate.py note <event_id> <one-two sentence description>
```

Right after a watch (or a snapshot triage you described to Mike), save the
transcript on the event. This is what makes later questions cheap.

## Interval questions ("what happened 3–4pm?")

```
python3 skills/frigate/frigate.py recap --since 15:00 --until 16:00        # today
python3 skills/frigate/frigate.py recap --since "2026-09-05T15:00" --until "2026-09-05T16:00"
```

`recap` lists every person event in the window with camera/time/duration,
marks the ones already watched+noted (✓ + their transcripts), and counts
per camera. Times are local to the house. From the recap:

- If a ✓ event covers it, answer from the stored note — no re-watching.
- If the unwatched events are routine-looking (usual cameras, short
  durations, the usual pattern), say so rather than watching everything —
  a 150-event afternoon of back-camera clips is not worth re-watching.
- Watch selectively (only genuinely unclear or interesting ones), note each
  after, then summarise the interval in one message.

## Ops

- `frigate.py status` shows daemon state, staleness, outbox backlog, cache
  size. If the daemon is down: `systemctl --user restart bg-frigate-watchd`
  and mention it to Mike.
- Logs: `.bg/logs/frigate-watchd.log`.
- Feed tuning (cooldowns, hours, wake level) is `skills/frigate/config.toml`
  — the daemon re-reads it every cycle, no restart needed.
- Camera state lives in the skill's own `state.db` (events, outbox, cursor);
  cached snapshots/clips under `skills/frigate/cache/` (auto-pruned).

## Verification (offline, no NVR needed)

```
python3 -m unittest discover -s skills/frigate/tests
```
