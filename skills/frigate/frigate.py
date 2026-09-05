#!/usr/bin/env python3
"""Frigate NVR CLI — Bob's read-only window into the local Frigate instance.

GET-only by construction: ``Frigate._get`` is the sole HTTP method in this
file. No delete/retain/PTZ/config-write verbs exist here, ever; mutating the
NVR stays an operator task in the Frigate UI.

Config comes from ``config.toml`` beside this script (env overrides:
FRIGATE_URL, FRIGATE_USER, FRIGATE_PASS, FRIGATE_STATE_DB). Optional basic
auth lives in an ``auth`` file (mode 600, ``user:pass``) when the NVR sits
behind a proxy. Shared state lives in ``state.db`` — the daemon (watchd.py)
imports this module for the client, config, and state helpers.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SKILL_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SKILL_DIR / "config.toml"
FFMPEG = "/home/linuxbrew/.linuxbrew/bin/ffmpeg"
FFPROBE = "/home/linuxbrew/.linuxbrew/bin/ffprobe"
# fall back to PATH lookups (docker instances brew-path differs)
if not Path(FFMPEG).exists():
    import shutil
    FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
    FFPROBE = shutil.which("ffprobe") or "ffprobe"


class FrigateError(Exception):
    """Operator-facing error (unreachable NVR, 404, bad auth)."""


# ── config ────────────────────────────────────────────────────────────

def _default_config() -> dict:
    return {
        "url": "http://127.0.0.1:5000",
        "user": "", "pass": "",
        "poll_s": 30,
        "labels": ["person"],
        "cameras": [],
        "timezone": "Australia/Perth",
        "backfill_min": 15,
        "max_backfill_s": 3600,
        "overlap_s": 300,
        "feed": {"enabled": False, "level": "info",
                 "ttl_action_s": 900, "ttl_info_s": 600},
        "rules": {"action_zones": [], "action_hours": "00:00-23:59",
                  "cooldown_min": 10, "min_duration_s": 2.0,
                  "action_budget_per_hour": 6},
        "cache": {"snapshot_prune_days": 7, "clip_prune_days": 2,
                  "max_clip_mb": 200},
        "health": {"stale_action_min": 30, "stale_repeat_info_min": 60},
        "watch": {"out_width": 854, "out_fps": 5, "max_s": 120,
                  "crf": 30, "keyframes": 2, "sheet_tile_px": 480},
    }


def load_config(path: Path | None = None) -> dict:
    """Merge config.toml over defaults, then env overrides. Missing file,
    missing keys, or wrong types all fall back silently to defaults — a
    broken config must never take the feed down."""
    cfg = _default_config()
    path = path or Path(os.environ.get("FRIGATE_CONFIG", DEFAULT_CONFIG_PATH))
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        raw = {}
    for key, val in raw.items():
        if isinstance(val, dict):
            if not isinstance(cfg.get(key), dict):
                cfg[key] = {}
            cfg[key].update({k: v for k, v in val.items()})
        else:
            cfg[key] = val
    if os.environ.get("FRIGATE_URL"):
        cfg["url"] = os.environ["FRIGATE_URL"]
    if os.environ.get("FRIGATE_USER"):
        cfg["user"] = os.environ["FRIGATE_USER"]
    if os.environ.get("FRIGATE_PASS"):
        cfg["pass"] = os.environ["FRIGATE_PASS"]
    cfg["url"] = str(cfg["url"]).rstrip("/")
    return cfg


def state_db_path(cfg: dict) -> Path:
    return Path(os.environ.get("FRIGATE_STATE_DB", SKILL_DIR / "state.db"))


def cache_dir() -> Path:
    return Path(os.environ.get("FRIGATE_CACHE_DIR", SKILL_DIR / "cache"))


def local_tz(cfg: dict) -> ZoneInfo:
    try:
        return ZoneInfo(str(cfg.get("timezone", "Australia/Perth")))
    except Exception:
        return ZoneInfo("Australia/Perth")


def fmt_local(ts: float | None, cfg: dict) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(float(ts), local_tz(cfg)).strftime("%d %b %H:%M:%S")


# ── state.db (the skill's own sqlite — deliberately NOT bob.db) ───────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  event_id TEXT PRIMARY KEY, camera TEXT, label TEXT, sub_label TEXT,
  start_time REAL, end_time REAL, zones_json TEXT, score REAL, top_score REAL,
  has_clip INTEGER, has_snapshot INTEGER, snapshot_path TEXT,
  action_sent INTEGER DEFAULT 0, closed_sent INTEGER DEFAULT 0,
  first_seen INTEGER, last_seen INTEGER);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_time);
CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created INTEGER, attempts INTEGER DEFAULT 0, next_try INTEGER DEFAULT 0,
  payload_json TEXT NOT NULL, dedup_key TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending');
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(status, next_try);
CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY, v TEXT);
"""

# per-event viewing notes ("transcripts") — added 2026-09-05, applied with a
# guarded ALTER so existing state.db files migrate in place
_SCHEMA_MIGRATIONS = [
    "ALTER TABLE events ADD COLUMN note TEXT",
    "ALTER TABLE events ADD COLUMN noted_at REAL",
]


def db(cfg: dict) -> sqlite3.Connection:
    conn = sqlite3.connect(state_db_path(cfg))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    for stmt in _SCHEMA_MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already there
    conn.commit()
    return conn


def state_get(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT v FROM state WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default


def state_set(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute("INSERT INTO state(k,v) VALUES(?,?) "
                 "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, str(value)))
    conn.commit()


# ── the one HTTP method ───────────────────────────────────────────────

class Frigate:
    """Read-only Frigate client. ``_get`` is the only verb that exists."""

    def __init__(self, cfg: dict, timeout: float = 10.0):
        self.cfg = cfg
        self.base = cfg["url"]
        self.timeout = timeout
        self._auth_header = None
        user, password = cfg.get("user") or "", cfg.get("pass") or ""
        if not (user and password):
            auth_file = SKILL_DIR / "auth"
            if auth_file.exists():
                parts = auth_file.read_text(encoding="utf-8").strip().split(":", 1)
                if len(parts) == 2:
                    user, password = parts
        if user and password:
            import base64
            token = base64.b64encode(f"{user}:{password}".encode()).decode()
            self._auth_header = f"Basic {token}"

    def _get(self, path: str, params: dict | None = None, raw: bool = False):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method="GET")
        if self._auth_header:
            req.add_header("Authorization", self._auth_header)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = resp.read()
        except urllib.error.HTTPError as e:
            raise FrigateError(f"frigate {path}: HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise FrigateError(f"frigate unreachable ({self.base}): {e.reason}") from e
        if raw:
            return data
        try:
            return json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise FrigateError(f"frigate {path}: bad JSON: {e}") from e

    # typed helpers (all funnel through _get)
    def version(self) -> str:
        url = self.base + "/api/version"
        req = urllib.request.Request(url, method="GET")
        if self._auth_header:
            req.add_header("Authorization", self._auth_header)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8").strip()
        except urllib.error.HTTPError as e:
            raise FrigateError(f"frigate /api/version: HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise FrigateError(f"frigate unreachable ({self.base}): {e.reason}") from e

    def config(self) -> dict:
        return self._get("/api/config")

    def events(self, *, after: float | None = None, before: float | None = None,
               camera: str | None = None, label: str | None = None,
               zone: str | None = None, has_clip: bool | None = None,
               limit: int = 100) -> list[dict]:
        params: dict = {"limit": limit, "include_thumbnails": 0}
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before
        if camera:
            params["camera"] = camera
        if label:
            params["label"] = label
        if zone:
            params["zone"] = zone
        if has_clip is not None:
            params["has_clip"] = 1 if has_clip else 0
        return self._get("/api/events", params)

    def snapshot_jpg(self, event_id: str) -> bytes:
        return self._get(f"/api/events/{event_id}/snapshot.jpg", raw=True)

    def clip_mp4(self, event_id: str) -> bytes:
        return self._get(f"/api/events/{event_id}/clip.mp4", raw=True)

    def latest_jpg(self, camera: str) -> bytes:
        return self._get(f"/api/{camera}/latest.jpg", raw=True)


def norm_event(e: dict) -> dict:
    """Normalise a Frigate 0.14 event row: score/top_score nest under
    ``data``; keep flat fields as a fallback for other versions."""
    data = e.get("data") or {}
    return {
        "event_id": str(e.get("id", "")),
        "camera": e.get("camera", ""),
        "label": e.get("label", ""),
        "sub_label": e.get("sub_label"),
        "start_time": e.get("start_time"),
        "end_time": e.get("end_time"),
        "zones": e.get("zones") or e.get("entered_zones") or [],
        "score": e.get("score") if e.get("score") is not None else data.get("score"),
        "top_score": (e.get("top_score") if e.get("top_score") is not None
                      else data.get("top_score")),
        "has_clip": bool(e.get("has_clip")),
        "has_snapshot": bool(e.get("has_snapshot")),
    }


# ── snapshot cache ────────────────────────────────────────────────────

def snapshot_cache_path(event_id: str) -> Path:
    month = datetime.now(dt_timezone.utc).strftime("%Y%m")
    return cache_dir() / "snapshots" / month / f"{event_id}.jpg"


def cache_snapshot(api: Frigate, ev: dict, cfg: dict) -> str | None:
    """Fetch and cache an event's snapshot; returns workspace-relative path
    or None. Called by the daemon BEFORE enqueueing an envelope so the path
    in the summary always exists (Frigate purges by retention; the cache
    is the hedge)."""
    path = snapshot_cache_path(ev["event_id"])
    if path.exists() and path.stat().st_size > 0:
        return rel_to_workspace(path)
    try:
        data = api.snapshot_jpg(ev["event_id"])
    except FrigateError:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return rel_to_workspace(path)


def rel_to_workspace(p: Path) -> str:
    try:
        rel = p.resolve().relative_to(Path("/home/bob/workspace"))
        return str(rel)
    except ValueError:
        return str(p)


# ── envelope construction (shared with watchd; tests drive these) ─────

def event_summary(ev: dict, snap_rel: str | None, tz: ZoneInfo | None = None) -> str:
    parts = []
    if ev.get("end_time") and ev.get("start_time"):
        parts.append(f"{int(ev['end_time'] - ev['start_time'])}s")
    score = ev.get("top_score") or ev.get("score")
    if score:
        parts.append(f"score {score:.2f}")
    zones = ",".join(ev.get("zones") or [])
    if zones:
        parts.append(zones)
    detail = f" ({', '.join(parts)})" if parts else ""
    hhmm = datetime.fromtimestamp(
        float(ev["start_time"]), tz or dt_timezone.utc).strftime("%H:%M") if ev.get("start_time") else "?"
    snap = f" snap={snap_rel}" if snap_rel else ""
    clip = "clip=y" if ev.get("has_clip") else "clip=n"
    label = ev.get("sub_label") or ev.get("label", "object")
    return f"{label} at {ev.get('camera', '?')} {hhmm}{detail} {clip}{snap}"


def build_envelope(ev: dict, *, level: str, snap_rel: str | None,
                   emission: str, ttl_s: int, extra_body: dict | None = None,
                   tz: ZoneInfo | None = None) -> dict:
    dedup = f"frigate:{ev['event_id']}" if emission == "sighting" \
        else f"frigate:{ev['event_id']}:close"
    body = dict(ev)
    body["snapshot_path"] = snap_rel
    body["emission"] = emission
    if extra_body:
        body.update(extra_body)
    return {
        "source": "frigate",
        "type": f"activity.{ev.get('label', 'object')}",
        "level": level,
        "dedup_key": dedup,
        "ttl_s": ttl_s,
        "target_hint": "frigate",
        "summary": event_summary(ev, snap_rel, tz),
        "body": body,
    }


# ── CLI ───────────────────────────────────────────────────────────────

def _parse_when(text: str, tz: ZoneInfo) -> float:
    """'2h', '45m', '90s', '3d' (ago) or ISO datetime → unix seconds.
    Naive ISO times are interpreted in the configured local timezone —
    '15:00' means 3pm at the house, not UTC."""
    text = text.strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if text and text[-1] in mult and text[:-1].isdigit():
        return time.time() - int(text[:-1]) * mult[text[-1]]
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.timestamp()


def _api(args) -> Frigate:
    cfg = load_config()
    if args.base:
        cfg["url"] = args.base
    if getattr(args, "user", None):
        cfg["user"] = args.user
    if getattr(args, "pass_", None):
        cfg["pass"] = args.pass_
    return Frigate(cfg, timeout=args.timeout)


def cmd_cameras(args) -> int:
    api = _api(args)
    cfg = load_config()
    conf = api.config()
    cams = conf.get("cameras", {})
    rows = []
    for name in sorted(cams):
        cam = cams[name] or {}
        zones = ",".join(sorted((cam.get("zones") or {}).keys()))
        rows.append({"camera": name, "zones": zones,
                     "enabled": cam.get("enabled", True),
                     "detect_enabled": cam.get("detect", {}).get("enabled", True)})
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    print(f"{'camera':<12} {'zones':<20} enabled detect")
    for r in rows:
        print(f"{r['camera']:<12} {r['zones'] or '-':<20} {str(r['enabled']):<8} {r['detect_enabled']}")
    print(f"\nfrigate {api.version()} at {cfg['url']} (tz {cfg['timezone']})")
    return 0


def cmd_events(args) -> int:
    api = _api(args)
    cfg = load_config()
    tz = local_tz(cfg)
    after = _parse_when(args.since, tz) if args.since else time.time() - 3600
    kwargs: dict = {"after": after, "limit": args.limit}
    if getattr(args, "until", None):
        kwargs["before"] = _parse_when(args.until, tz)
    if args.camera:
        kwargs["camera"] = args.camera
    if args.label:
        kwargs["label"] = args.label
    if args.zone:
        kwargs["zone"] = args.zone
    if args.has_clip:
        kwargs["has_clip"] = True
    evs = [norm_event(e) for e in api.events(**kwargs)]
    # mark locally-cached snapshots
    conn = db(cfg)
    for ev in evs:
        row = conn.execute("SELECT snapshot_path FROM events WHERE event_id=?",
                           (ev["event_id"],)).fetchone()
        ev["cached_snap"] = (row["snapshot_path"] if row else None) or (
            rel_to_workspace(snapshot_cache_path(ev["event_id"]))
            if snapshot_cache_path(ev["event_id"]).exists() else None)
    conn.close()
    if args.json:
        print(json.dumps(evs, indent=2))
        return 0
    print(f"{'time':<17} {'camera':<10} {'label':<8} {'dur':>5} {'score':>5} clip snap")
    for ev in reversed(evs):  # oldest first for reading
        dur = (int(ev["end_time"] - ev["start_time"])
               if ev.get("end_time") and ev.get("start_time") else 0)
        score = ev.get("top_score") or ev.get("score") or 0
        print(f"{fmt_local(ev['start_time'], cfg):<17} {ev['camera']:<10} "
              f"{ev['label']:<8} {dur:>4}s {score:>5.2f} "
              f"{'y' if ev['has_clip'] else 'n':>4} "
              f"{'cached' if ev['cached_snap'] else '-'}")
    print(f"\n{len(evs)} events. watch one: python3 skills/frigate/frigate.py watch <event_id>")
    return 0


def cmd_event(args) -> int:
    api = _api(args)
    cfg = load_config()
    conn = db(cfg)
    row = conn.execute("SELECT * FROM events WHERE event_id=?",
                       (args.event_id,)).fetchone()
    conn.close()
    if row:
        ev = dict(row)
        ev["zones"] = json.loads(ev.pop("zones_json") or "[]")
    else:
        evs = api.events(limit=500)
        match = [norm_event(e) for e in evs if str(e.get("id")) == args.event_id]
        if not match:
            print(f"event {args.event_id} not found in state or recent history")
            return 1
        ev = match[0]
    print(json.dumps(ev, indent=2, default=str))
    return 0


def _fetch_clip(api: Frigate, event_id: str, cfg: dict) -> Path:
    """Download (or reuse) the cached clip. Raises FrigateError on 404."""
    clip = cache_dir() / "clips" / f"{event_id}.mp4"
    if clip.exists() and clip.stat().st_size > 0:
        return clip
    max_bytes = int(cfg["cache"]["max_clip_mb"]) * 1024 * 1024
    data = api.clip_mp4(event_id)
    if len(data) > max_bytes:
        raise FrigateError(f"clip is {len(data)} bytes, over the "
                           f"{cfg['cache']['max_clip_mb']}MB cap")
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(data)
    return clip


def _ffprobe_json(path: Path) -> dict:
    out = subprocess.run(
        [FFPROBE, "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise FrigateError(f"ffprobe failed on {path}: {out.stderr[:200]}")
    return json.loads(out.stdout or "{}")


def cmd_snapshot(args) -> int:
    api = _api(args)
    ev = {"event_id": args.event_id}
    snap_rel = cache_snapshot(api, ev, load_config())
    if not snap_rel:
        print(f"snapshot unavailable for {args.event_id} (purged or 404)")
        return 1
    path = Path("/home/bob/workspace") / snap_rel
    if args.out:
        import shutil
        shutil.copy(path, args.out)
        path = Path(args.out)
    print(path)
    return 0


def cmd_live(args) -> int:
    api = _api(args)
    cfg = load_config()
    if not args.camera:
        conf = api.config()
        args.camera = sorted((conf.get("cameras") or {}).keys())[0]
    data = api.latest_jpg(args.camera)
    out = Path(args.out or f"/tmp/frigate-live-{args.camera}.jpg")
    out.write_bytes(data)
    print(out)
    print(f"live frame from {args.camera} ({cfg['url']}) — view with read_image")
    return 0


def cmd_watch(args) -> int:
    api = _api(args)
    cfg = load_config()
    w = cfg["watch"]
    conn = db(cfg)
    row = conn.execute("SELECT * FROM events WHERE event_id=?",
                       (args.event_id,)).fetchone()
    conn.close()

    snap_path = snapshot_cache_path(args.event_id)
    snap_rel = rel_to_workspace(snap_path) if snap_path.exists() else None
    if not snap_rel:
        snap_rel = cache_snapshot(api, {"event_id": args.event_id}, cfg)

    meta = ""
    if row:
        dur = (row["end_time"] - row["start_time"]) if row["end_time"] else 0
        meta = (f"event  {row['event_id']}  {row['camera']} {row['label']} "
                f"{fmt_local(row['start_time'], cfg)} ({int(dur)}s)")
        print(meta)

    # snapshot-only fallback when the clip is gone
    try:
        clip = _fetch_clip(api, args.event_id, cfg)
    except FrigateError as e:
        print(f"CLIP_GONE ({e})")
        if snap_rel:
            print(f"snap   {Path('/home/bob/workspace') / snap_rel}")
            print("read_image the snapshot; describe what you can from it.")
        return 1

    probe = _ffprobe_json(clip)
    try:
        duration = float(probe["format"]["duration"])
    except (KeyError, ValueError, TypeError):
        duration = 0.0
    print(f"clip   {clip} ({clip.stat().st_size / 1e6:.1f} MB, {duration:.0f}s)")

    frames_dir = cache_dir() / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    if args.stills:
        _make_stills(clip, args.event_id, cfg, frames_dir, args.keyframes or w["keyframes"])
        print(f"snap   {Path('/home/bob/workspace') / snap_rel}" if snap_rel else "snap   (none)")
        print("read_image order: sheet → snap → keyframes (≤4 images).")
        return 0

    watch_clip = frames_dir / f"{args.event_id}-watch.mp4"
    vf = (f"scale='min({int(w['out_width'])},iw)':-2,fps={int(w['out_fps'])}")
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-i", str(clip),
           "-t", str(int(w["max_s"])), "-vf", vf, "-an",
           "-c:v", "libx264", "-crf", str(int(w["crf"])), str(watch_clip)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        print(f"transcode failed: {proc.stderr[:300]}")
        return 1
    wprobe = _ffprobe_json(watch_clip)
    w_dur = float(wprobe.get("format", {}).get("duration") or 0)
    print(f"watch  {watch_clip} ({watch_clip.stat().st_size / 1e6:.1f} MB, "
          f"{w_dur:.0f}s, {w['out_width']}px@{w['out_fps']}fps)")
    if snap_rel:
        print(f"snap   {Path('/home/bob/workspace') / snap_rel}")
    print(f"\nread_video {watch_clip}")
    print("then optionally: skills/faces/.venv/bin/python skills/faces/faces.py whois "
          f"{Path('/home/bob/workspace') / snap_rel if snap_rel else '<snap>'}")
    print("If read_video says the model cannot watch video, rerun this with --stills.")
    return 0


def _make_stills(clip: Path, event_id: str, cfg: dict, frames_dir: Path,
                 keyframes: int) -> None:
    w = cfg["watch"]
    tile = int(w["sheet_tile_px"])
    probe = _ffprobe_json(clip)
    try:
        duration = float(probe["format"]["duration"])
    except (KeyError, ValueError, TypeError):
        duration = 0.0
    sheet = frames_dir / f"{event_id}-sheet.jpg"
    # 9 frames evenly sampled across the clip: sample rate = 9 / duration
    fps = 9.0 / max(duration, 0.5)
    proc = subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error", "-i", str(clip),
         "-frames:v", "1", "-vf", f"fps={fps:.4f},scale={tile}:-2,tile=3x3",
         "-q:v", "3", str(sheet)],
        capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        print(f"sheet failed: {proc.stderr[:200]}")
    else:
        print(f"sheet  {sheet} (9 frames evenly sampled @1/{1/fps:.1f}s, {tile}px tiles)")
    for i in range(1, keyframes + 1):
        at = duration * (i + 1) / (keyframes + 2)
        kf = frames_dir / f"{event_id}-k{i}.jpg"
        proc = subprocess.run(
            [FFMPEG, "-y", "-loglevel", "error", "-ss", f"{at:.2f}", "-i", str(clip),
             "-frames:v", "1", "-vf", f"scale={int(w['out_width'])}:-2", "-q:v", "3", str(kf)],
            capture_output=True, text=True, timeout=120)
        if proc.returncode == 0:
            print(f"key   {kf} @{at:.0f}s")


def cmd_note(args) -> int:
    """Attach Bob's viewing transcript to an event — durable where chat
    history is not. Run after watching: `note <id> <what happened>`."""
    api = _api(args)
    cfg = load_config()
    conn = db(cfg)
    row = conn.execute("SELECT event_id FROM events WHERE event_id=?",
                       (args.event_id,)).fetchone()
    if not row:
        # event predates daemon tracking — pull it from the live API
        for raw in api.events(limit=500):
            if str(raw.get("id")) == args.event_id:
                upsert_row = norm_event(raw)
                conn.execute(
                    """INSERT INTO events(event_id, camera, label, sub_label,
                         start_time, end_time, zones_json, score, top_score,
                         has_clip, has_snapshot, first_seen, last_seen)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (upsert_row["event_id"], upsert_row["camera"], upsert_row["label"],
                     upsert_row.get("sub_label"), upsert_row["start_time"],
                     upsert_row["end_time"], json.dumps(upsert_row.get("zones") or []),
                     upsert_row.get("score"), upsert_row.get("top_score"),
                     int(upsert_row["has_clip"]), int(upsert_row["has_snapshot"]),
                     int(time.time()), int(time.time())))
                break
        else:
            print(f"event {args.event_id} not found in state or recent history")
            conn.close()
            return 1
    text = " ".join(args.text).strip()
    conn.execute("UPDATE events SET note=?, noted_at=? WHERE event_id=?",
                 (text, time.time(), args.event_id))
    conn.commit()
    conn.close()
    print(f"noted {args.event_id}: {text[:120]}")
    return 0


def cmd_recap(args) -> int:
    """Interval summary: what the cameras saw, plus what Bob already watched
    and concluded (stored notes). The answer to 'what happened 3-4pm?'."""
    api = _api(args)
    cfg = load_config()
    tz = local_tz(cfg)
    after = _parse_when(args.since, tz) if args.since else time.time() - 3600
    kwargs: dict = {"after": after, "limit": max(args.limit, 200)}
    if getattr(args, "until", None):
        kwargs["before"] = _parse_when(args.until, tz)
    if args.camera:
        kwargs["camera"] = args.camera
    evs = [norm_event(e) for e in api.events(**kwargs)]
    evs.sort(key=lambda e: e["start_time"] or 0)

    conn = db(cfg)
    notes = {}
    for eid in [e["event_id"] for e in evs]:
        r = conn.execute("SELECT note FROM events WHERE event_id=?", (eid,)).fetchone()
        if r and r["note"]:
            notes[eid] = r["note"]
    conn.close()

    if args.json:
        out = [dict(e, note=notes.get(e["event_id"])) for e in evs]
        print(json.dumps(out, indent=2))
        return 0

    from collections import Counter
    cams = Counter(e["camera"] for e in evs)
    start_s = fmt_local(evs[0]["start_time"], cfg) if evs else "-"
    end_s = fmt_local(evs[-1]["end_time"] or evs[-1]["start_time"], cfg) if evs else "-"
    watched = sum(1 for e in evs if e["event_id"] in notes)
    print(f"{len(evs)} person events, {start_s} → {end_s} "
          f"({', '.join(f'{c} {n}' for c, n in cams.most_common())})")
    print(f"{watched} already watched+noted, {len(evs) - watched} unwatched "
          f"(clips on the NVR until retention purges them)\n")
    for e in evs:
        dur = (int(e["end_time"] - e["start_time"])
               if e.get("end_time") and e.get("start_time") else 0)
        hhmm = datetime.fromtimestamp(float(e["start_time"]), tz).strftime("%H:%M")
        mark = "✓" if e["event_id"] in notes else " "
        note = f"\n         └ noted: {notes[e['event_id']]}" if e["event_id"] in notes else ""
        print(f"{mark} {hhmm} {e['camera']:<9} {dur:>3}s "
              f"{e['event_id'].split('-')[0]}{note}")
    if evs:
        print("\nwatch one: python3 skills/frigate/frigate.py watch <event_id>  "
              "(then note it: note <event_id> <description>)")
    return 0


def cmd_status(args) -> int:
    cfg = load_config()
    health_file = SKILL_DIR / "run" / "health.json"
    health = {}
    if health_file.exists():
        try:
            health = json.loads(health_file.read_text())
        except json.JSONDecodeError:
            health = {"error": "health.json unreadable"}
    else:
        health = {"error": "no health.json — daemon never ran"}
    try:
        version = _api(args).version()
    except FrigateError as e:
        version = f"UNREACHABLE ({e})"
    conn = db(cfg)
    cursor = state_get(conn, "cursor")
    pending = conn.execute(
        "SELECT COUNT(*) c FROM outbox WHERE status='pending'").fetchone()["c"]
    conn.close()
    unit = subprocess.run(["systemctl", "--user", "is-active", "bg-frigate-watchd"],
                          capture_output=True, text=True)
    unit_state = unit.stdout.strip() or "unknown"
    cache_bytes = sum(
        p.stat().st_size for p in cache_dir().rglob("*") if p.is_file())
    print(f"frigate     {version}")
    print(f"daemon      {unit_state} (health {json.dumps(health)[:200]})")
    if cursor:
        age_min = (time.time() - float(cursor)) / 60
        print(f"cursor      {float(cursor):.0f} ({age_min:.0f} min ago)")
    print(f"outbox      {pending} pending")
    print(f"cache       {cache_bytes / 1e6:.0f} MB")
    if health.get("phase") == "stale":
        print(f"STALE       frigate unreachable since "
              f"{fmt_local(float(health["stale_since"]) if health.get("stale_since") else None, cfg)}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", help="frigate base URL override")
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--user", help="basic-auth user override")
    p.add_argument("--pass", dest="pass_", help="basic-auth password override")
    p.add_argument("--json", action="store_true", help="raw JSON output")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("cameras", help="list cameras and zones")
    sp = sub.add_parser("events", help="list recent events")
    sp.add_argument("--json", action="store_true", help="raw JSON output")
    sp.add_argument("--camera"); sp.add_argument("--label"); sp.add_argument("--zone")
    sp.add_argument("--since", default="1h", help="e.g. 2h, 45m, 2026-09-05T00:00")
    sp.add_argument("--until", help="interval end — makes '3-4pm' queries possible")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--has-clip", action="store_true")
    sp = sub.add_parser("event", help="full record for one event")
    sp.add_argument("event_id")
    sp = sub.add_parser("snapshot", help="cache+print an event snapshot")
    sp.add_argument("event_id"); sp.add_argument("--out")
    sp = sub.add_parser("live", help="grab a live frame")
    sp.add_argument("camera", nargs="?"); sp.add_argument("--out")
    sp = sub.add_parser("watch", help="prepare a clip for read_video (or --stills)")
    sp.add_argument("event_id")
    sp.add_argument("--stills", action="store_true",
                    help="contact sheet + keyframes instead of a transcoded clip")
    sp.add_argument("--keyframes", type=int)
    sp = sub.add_parser("note", help="save your viewing transcript on an event")
    sp.add_argument("event_id"); sp.add_argument("text", nargs="+")
    sp = sub.add_parser("recap", help="interval summary with stored notes")
    sp.add_argument("--since", default="1h")
    sp.add_argument("--until")
    sp.add_argument("--camera")
    sp.add_argument("--limit", type=int, default=200)
    sp.add_argument("--json", action="store_true", help="raw JSON output")
    sub.add_parser("status", help="daemon + feed health")

    args = p.parse_args(argv)
    handlers = {"cameras": cmd_cameras, "events": cmd_events, "event": cmd_event,
                "snapshot": cmd_snapshot, "live": cmd_live, "watch": cmd_watch,
                "note": cmd_note, "recap": cmd_recap, "status": cmd_status}
    try:
        return handlers[args.cmd](args)
    except FrigateError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyError as e:
        print(f"Error: missing config key {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
