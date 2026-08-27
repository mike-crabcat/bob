# Bob FM — Spotify-backed radio station

Plan 2026-08-26, revised 2026-08-27 (no-service pivot, then Zotify decision).
Status: **station server + bg tools built and verified** (test-tone); next
step is Zotify setup, then queue mode.

## The idea

An always-on internet radio station on the home server, with Bob as DJ and
program director. Music comes from the Spotify Premium account: **Zotify**
fetches tracks at 1× realtime pace into a capped local library, and the
station plays a queue of local files — songs and Bob-voiced announcements
interleaved — to any player on the LAN or tailnet.

## Reality checks

1. **"SHOUTcast" → homegrown HTTP/ICY.** A ~450-line asyncio server
   replaces icecast at LAN/tailnet scale. No apt, no sudo, no system service.
2. **Spotify has no official re-streaming path.** Zotify (like spotifyd
   before it) is an unofficial client — ToS-breaking with a real but low
   account-ban risk. **Decision (Mike, 2026-08-27): Zotify in
   `DOWNLOAD_REAL_TIME` mode** — fetches at 1× playback pace, Zotify's own
   recommended ban-prevention mode, and the station never needs to be more
   than a few songs ahead of the air queue anyway. Mitigations: LAN+tailnet
   listeners only, no public funnel, capped library, no bulk grabs.
3. **Premium confirmed** (family sub, one account carries the station).
4. **Air format**: Zotify transcodes to MP3 (station bitrate) on download —
   the air path streams files byte-for-byte with **no live encoder**.

## Architecture

```
 CAPTURE (deck A)                     AIR (deck B)
 Zotify (pip, in ~/bobenv)            station.py queue mode:
   playlist/track URIs                  library/<id>.mp3 ─┐
   → MP3 256k, tagged                    ids/*.mp3 (TTS) ─┼→ queue manifest
   → library/ (capped ~2GB LRU)          (announcements)  ┘     │
   DOWNLOAD_REAL_TIME=true                                      ▼
                                                        sequential file pump
   spotifyd live mode (built,            → asyncio HTTP :8010 /stream /status
   optional "DJ desk" — play             → preroll burst, laggard kick
   from a phone, straight to air)

 CONTROL: bg_* tools spawn/stop/inspect everything as systemd-run
 transient units (bg-<name>.service) — nothing is a child or cgroup
 member of the bob unit, so frequent `systemctl --user restart bob`
 never touches the station.
 Listeners: http://192.168.100.10:8010/stream (LAN) ·
            http://100.73.201.65:8010/stream (tailnet)
```

### Pieces built (2026-08-27)

1. **bg_* process tools** — `services/process_tools.py`:
   bg_start/status/logs/stop/restart, registered in `build_common_tools`.
   `systemd-run --user` transient units + on-disk registry
   (`workspace/.bg/`); same sandbox filter as bash; logs `.bg/logs/`;
   setsid fallback. Tests: `tests/services/test_process_tools.py` (10 ✓).
2. **radio skill** — `workspace/skills/radio/`:
   - `station.py` — asyncio streaming server :8010 (preroll ring, laggard
     kick, `/status`, `run/status.json`, graceful SIGTERM), ffmpeg-based
     source pipeline with child supervision + silence injection, verified
     end-to-end in `--test-tone` mode (256k MP3, listener attach, ffprobe
     clean).
   - live mode (spotifyd pipe→FIFO→ffmpeg) built and unverified against
     real Spotify — kept as the DJ desk; needs the one-time
     `bin/spotifyd authenticate` only if/when wanted.
   - `radio.py` (now/listeners/status/stream-url/doctor), `bin/spotifyd`
     v0.4.2, `config.json`, `skill.md`.

### Pieces to build

3. **Zotify integration** (deck A) — pip install into ~/bobenv (house
   venv, per bash-tool convention), config in the skill dir if possible:
   `DOWNLOAD_REAL_TIME=true`, MP3 output at station bitrate, default
   `BULK_WAIT_TIME`. Output routed into `library/`. `radio.py fetch
   <playlist-or-track-uri>` wraps it. Library capped ~2GB with LRU
   eviction — a radio rotation buffer, not an export tool.
4. **Queue mode** (deck B) — station.py gains a source abstraction:
   `{live | queue | test}`. Queue mode reads a JSON queue manifest
   (`run/queue.json`: ordered items `{type: song|announcement, path,
   title, artist, requested_by?}`), pumps each file to air, advances on
   EOF + small gap; writes exact now-playing into `/status`. Queue empty →
   rotate library (or silence, configurable). No ffmpeg on this path.
5. **Announcements** — `radio.py announce "text"`: OpenAI TTS (key already
   in skill env) → MP3 into `ids/`, appended to the queue manifest as the
   next item. Clean gaps, no talkover. Later: station IDs every N tracks,
   jingle-of-the-day tie-in, weather inserts.
6. **Bob as program director** — fill the queue from playlists on a
   schedule (timer infra), request line via WhatsApp (needs the inbound DM
   gate opened for the station number), "next up by request of…"
   announcements. Phase 2's Spotify Web API (OAuth refresh token,
   absorbing the stale `spotify-playlists` skill) becomes optional here —
   useful for metadata/DJ control, not required for capture.

## Phases

- [x] 1. Transport (bg tools + station server) — done, test-tone verified.
- [x] 2. Zotify installed + configured. **v1.0-dev branch (commit b11ecfa)**
      — the release still uses password auth, which Spotify killed for
      third-party clients; dev uses OAuth PKCE (username prompt → click a
      link → browser auth → credentials cached). Own venv in the skill
      (pinned deps clash with bobenv); source preserved at `zotify-src/`
      with the broken self-hosted music-tag dep repointed to PyPI; pinned
      librespot ref from Pipfile.lock. v1.0 dropped DOWNLOAD_REAL_TIME, so
      `radio.py fetch` enforces the same 1x-listening-time footprint via a
      pacing ledger. **Done 2026-08-27**: Mike logged in (OAuth), fetch
      verified end-to-end (three API-compat patches carried in zotify-src
      for librespot-python @683d9e7 — older refs get Mercury 403).
- [x] 3. Queue mode — built and verified against a synthetic library:
      rotation (no back-to-back repeats), manifest preempts rotation,
      announcements air next, skip, consumed-manifest semantics with a
      cross-process lock, exact now-playing in /status. **First real
      broadcast 2026-08-27 20:40** — station on air (bg-bob-fm unit) with
      Zotify-fetched tracks.
- [x] 4. Announcements — `radio.py announce` (OpenAI TTS → re-encoded to
      air format → next queue item) + `announce-file` for pre-made audio.
      Not yet exercised against a live TTS key.
- [ ] 5. Programming: scheduling, station IDs, WhatsApp request line.
- [ ] 6. Remote: tailnet works day one. Public funnel: no.

### Verified (2026-08-27)

- bg tools: full lifecycle under systemd-run + setsid fallback, sandbox
  blocks, dead-entry replacement, restart; 10/10 tests; full bob-server
  suite 534 passed.
- Live/test pipeline (test-tone): listener attach, preroll + continuous
  audio (256k MP3, ffprobe clean), graceful SIGTERM. (First run caught a
  real bug: dataclass `__eq__` breaks set hashing — fixed with `eq=False`.)
- Queue mode (synthetic library): rotation without adjacent repeats,
  manifest preemption, announce-file airs next, skip advances mid-item,
  on_air/now_playing correct, drift-corrected realtime pacing.
- Zotify 0.6.14 in the skill's own venv (`protobuf==3.20.1` pin conflicts
  with bobenv's Google deps — bobenv left clean); zconfig parses (reaches
  the login prompt); realtime mode + MP3 256k set.

## Watch points

- **Ban risk** — Zotify realtime mode + library cap keeps the footprint
  near "just listening"; still, watch the account email, stop if warned.
  "Session terminated" error = re-login.
- **Zotify bitrot** — pin the version; Spotify protocol changes break it
  periodically.
- **Machine reboot = station down** until started again (the agreed
  not-a-service trade-off); Bob restarts are harmless.
- **Disk** — library cap enforced by the radio skill, not by hand.

## History

- 2026-08-26: original plan — icecast2 (apt/system) + librespot systemd unit.
- 2026-08-27 (a): no-service pivot — bg_* tools via systemd-run; station
  becomes a pure skill; icecast/sudo/systemd dropped.
- 2026-08-27 (b): Mike decides deck A = **Zotify in realtime download
  mode** (replacing realtime spotifyd capture as primary); queue mode
  becomes the main broadcast path; spotifyd kept as optional DJ desk.
