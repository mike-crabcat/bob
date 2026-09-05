#!/usr/bin/env python3
"""Frigate feed daemon — person-activity events into Bob's stimulus spine.

Polls the local Frigate NVR (read-only), tracks events across their
lifecycle, and posts envelopes to POST /api/v1/stimulus/events:

- **action** at first qualifying sighting (elapsed >= min_duration_s, inside
  action hours, camera cooldown clear, hourly budget left). This is the wake.
- **info** at close — the complete, queryable record. Level info never
  steers (spine rule), so close records are free.
- **health.stale** when the NVR becomes unreachable for stale_action_min.

Envelope level for sightings follows [feed] level ("info" during shadow,
"action" when live). State lives in the skill's own state.db — the events
table plus a PERSISTENT outbox (Frigate events don't regenerate after
retention purges, so a crash mid-outage must not drop alerts). Config is
re-read every cycle: tuning cooldowns/level never needs a restart.

Run (unit env-loads ~/config/.env for BOB_STIMULUS_TOKEN):
  cd /home/bob/workspace && exec python3 skills/frigate/watchd.py
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import frigate as fg  # noqa: E402

STOP = threading.Event()


# ── action hours ──────────────────────────────────────────────────────

def _parse_hhmm(s: str) -> int:
    h, m = s.strip().split(":")
    return int(h) * 60 + int(m)


def in_action_hours(now_local: datetime, window: str) -> bool:
    """'06:00-22:00' (same-day) or '22:00-06:00' (overnight wrap)."""
    try:
        start_s, end_s = window.split("-")
        start, end = _parse_hhmm(start_s), _parse_hhmm(end_s)
    except (ValueError, AttributeError):
        return True  # unparseable window never suppresses
    now = now_local.hour * 60 + now_local.minute
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end  # overnight wrap


# ── outbox poster (background thread) ─────────────────────────────────

class Poster(threading.Thread):
    """Drains the persistent outbox to the stimulus endpoint with backoff.
    4xx responses drop the row — a rejected envelope can't be retried into
    acceptance (cryptobro rule); connectivity failures retry forever."""

    def __init__(self, cfg: dict):
        super().__init__(daemon=True, name="poster")
        self.base = os.environ.get("BOB_API_BASE", "http://127.0.0.1:8420").rstrip("/")
        self.token = os.environ.get("BOB_STIMULUS_TOKEN", "")
        self.cfg = cfg

    def run(self) -> None:
        while not STOP.is_set():
            try:
                worked = self._drain()
            except Exception as e:  # never let the poster die
                print(f"poster error: {e}", flush=True)
                worked = 0
            STOP.wait(1.0 if worked else 5.0)

    def _drain(self) -> int:
        now = time.time()
        conn = fg.db(self.cfg)
        rows = conn.execute(
            "SELECT id, payload_json, attempts FROM outbox "
            "WHERE status='pending' AND next_try <= ? ORDER BY id LIMIT 20",
            (now,)).fetchall()
        conn.close()
        for row in rows:
            if not self.token:
                print("poster: BOB_STIMULUS_TOKEN empty — holding envelopes", flush=True)
                return 0
            payload = json.loads(row["payload_json"])
            try:
                self._post(payload)
                conn = fg.db(self.cfg)
                conn.execute("UPDATE outbox SET status='sent' WHERE id=?", (row["id"],))
                conn.commit(); conn.close()
                print(f"posted {payload['type']} {payload['dedup_key']}", flush=True)
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode()[:200]
                except Exception:
                    pass
                status = "dropped" if 400 <= e.code < 500 and e.code != 429 else "pending"
                delay = 0 if status == "dropped" else min(60, 2 ** min(row["attempts"] + 1, 6))
                conn = fg.db(self.cfg)
                conn.execute(
                    "UPDATE outbox SET status=?, attempts=attempts+1, next_try=? WHERE id=?",
                    (status, time.time() + delay, row["id"]))
                conn.commit(); conn.close()
                print(f"poster HTTP {e.code} {body} → {status}", flush=True)
                if status == "dropped":
                    continue
                return 0
            except urllib.error.URLError as e:
                delay = min(60, 2 ** min(row["attempts"] + 1, 6))
                conn = fg.db(self.cfg)
                conn.execute(
                    "UPDATE outbox SET attempts=attempts+1, next_try=? WHERE id=?",
                    (time.time() + delay, row["id"]))
                conn.commit(); conn.close()
                print(f"poster unreachable: {e.reason} — retry in {delay}s", flush=True)
                return 0
        return len(rows)

    def _post(self, payload: dict) -> None:
        req = urllib.request.Request(
            self.base + "/api/v1/stimulus/events",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()


def enqueue(conn: sqlite3.Connection, envelope: dict) -> None:
    conn.execute(
        "INSERT INTO outbox(created, next_try, payload_json, dedup_key) "
        "VALUES(?,?,?,?)",
        (int(time.time()), time.time(), json.dumps(envelope), envelope["dedup_key"]))
    conn.commit()


# ── the poll cycle ────────────────────────────────────────────────────

def upsert_event(conn: sqlite3.Connection, ev: dict) -> None:
    now = int(time.time())
    conn.execute(
        """INSERT INTO events(event_id, camera, label, sub_label, start_time, end_time,
             zones_json, score, top_score, has_clip, has_snapshot,
             first_seen, last_seen)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(event_id) DO UPDATE SET
             camera=excluded.camera, label=excluded.label,
             sub_label=excluded.sub_label, end_time=excluded.end_time,
             zones_json=excluded.zones_json, score=excluded.score,
             top_score=excluded.top_score, has_clip=excluded.has_clip,
             has_snapshot=excluded.has_snapshot, last_seen=excluded.last_seen""",
        (ev["event_id"], ev["camera"], ev["label"], ev.get("sub_label"),
         ev["start_time"], ev["end_time"], json.dumps(ev.get("zones") or []),
         ev.get("score"), ev.get("top_score"), int(ev["has_clip"]),
         int(ev["has_snapshot"]), now, now))
    conn.commit()


def classify_and_emit(conn: sqlite3.Connection, api: fg.Frigate, cfg: dict) -> int:
    """One pass over tracked rows: emit action at qualifying sighting,
    info at close. Returns envelopes enqueued."""
    feed = cfg["feed"]
    rules = cfg["rules"]
    tz = fg.local_tz(cfg)
    now = time.time()
    max_age = now - float(cfg["max_backfill_s"])
    emitted = 0

    rows = conn.execute(
        "SELECT * FROM events WHERE last_seen > ? - 7200 ORDER BY start_time",
        (now,)).fetchall()
    for row in rows:
        if row["start_time"] < max_age:
            continue  # post-outage bound: old alerts are noise
        ev = {
            "event_id": row["event_id"], "camera": row["camera"],
            "label": row["label"], "sub_label": row["sub_label"],
            "start_time": row["start_time"], "end_time": row["end_time"],
            "zones": json.loads(row["zones_json"] or "[]"),
            "score": row["score"], "top_score": row["top_score"],
            "has_clip": bool(row["has_clip"]), "has_snapshot": bool(row["has_snapshot"]),
        }
        snap_rel = row["snapshot_path"]

        # action: first sighting old enough to clear the blip filter
        if not row["action_sent"] and feed["enabled"]:
            elapsed = (row["end_time"] or now) - row["start_time"]
            if elapsed >= float(rules["min_duration_s"]):
                cams = cfg.get("cameras") or []
                zones_cfg = [str(z).lower() for z in (rules.get("action_zones") or [])]
                cams_ok = not cams or row["camera"] in cams
                zone_ok = (not zones_cfg or
                           any(f"{row['camera']}:{z}".lower() in zones_cfg
                               for z in ev["zones"]))
                hours_ok = in_action_hours(datetime.now(tz), str(rules["action_hours"]))
                hour_key = f"budget:{datetime.now(tz).strftime('%Y%m%d%H')}"
                used = int(fg.state_get(conn, hour_key, "0") or 0)
                cd_key = f"cd:{row['camera']}"
                last_cd = float(fg.state_get(conn, cd_key, "0") or 0)
                cooled = now - last_cd >= float(rules["cooldown_min"]) * 60
                budget_left = used < int(rules["action_budget_per_hour"])
                if cams_ok and zone_ok and hours_ok and cooled:
                    # over-budget: keep the trace, lose the wake
                    level = feed["level"] if budget_left else "info"
                    extra = {} if budget_left else {"downgraded": "budget"}
                    if not snap_rel and ev["has_snapshot"]:
                        snap_rel = fg.cache_snapshot(api, ev, cfg)
                        if snap_rel:
                            conn.execute("UPDATE events SET snapshot_path=? WHERE event_id=?",
                                         (snap_rel, row["event_id"]))
                            conn.commit()
                    ttl = int(feed["ttl_action_s"] if level == "action" else feed["ttl_info_s"])
                    enqueue(conn, fg.build_envelope(
                        ev, level=level, snap_rel=snap_rel,
                        emission="sighting", ttl_s=ttl, extra_body=extra, tz=tz))
                    if level == "action":
                        fg.state_set(conn, cd_key, int(now))
                        fg.state_set(conn, hour_key, used + 1)
                    conn.execute("UPDATE events SET action_sent=1 WHERE event_id=?",
                                 (row["event_id"],))
                    conn.commit()
                    emitted += 1

        # info: the complete record at close
        if row["end_time"] and not row["closed_sent"] and feed["enabled"]:
            if not snap_rel and ev["has_snapshot"]:
                snap_rel = fg.cache_snapshot(api, ev, cfg)
                if snap_rel:
                    conn.execute("UPDATE events SET snapshot_path=? WHERE event_id=?",
                                 (snap_rel, row["event_id"]))
                    conn.commit()
            enqueue(conn, fg.build_envelope(
                ev, level="info", snap_rel=snap_rel,
                emission="close", ttl_s=int(feed["ttl_info_s"]), tz=tz))
            conn.execute("UPDATE events SET closed_sent=1 WHERE event_id=?",
                         (row["event_id"],))
            conn.commit()
            emitted += 1
    return emitted


def prune(cfg: dict) -> None:
    now = time.time()
    conn = fg.db(cfg)
    last = float(fg.state_get(conn, "last_prune", "0") or 0)
    if now - last < 6 * 3600:
        conn.close()
        return
    fg.state_set(conn, "last_prune", int(now))
    conn.execute("DELETE FROM outbox WHERE status != 'pending' AND created < ?",
                 (now - 86400,))
    conn.commit()
    conn.close()
    snap_days = float(cfg["cache"]["snapshot_prune_days"])
    clip_days = float(cfg["cache"]["clip_prune_days"])
    for sub, days in (("snapshots", snap_days), ("clips", clip_days), ("frames", clip_days)):
        d = fg.cache_dir() / sub
        if not d.exists():
            continue
        for p in d.rglob("*"):
            if p.is_file() and now - p.stat().st_mtime > days * 86400:
                p.unlink(missing_ok=True)


def write_health(cfg: dict, phase: str, stale_since: float | None,
                 cursor: float | None, error: str = "") -> None:
    conn = fg.db(cfg)
    pending = conn.execute(
        "SELECT COUNT(*) c FROM outbox WHERE status='pending'").fetchone()["c"]
    tracked = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    conn.close()
    health = {
        "ts": int(time.time()), "phase": phase,
        "stale_since": stale_since, "cursor": cursor,
        "events_tracked": tracked, "outbox_pending": pending,
        "feed_enabled": cfg["feed"]["enabled"], "level": cfg["feed"]["level"],
        "last_error": error[:300],
    }
    path = fg.SKILL_DIR / "run" / "health.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(health, indent=2))


def main() -> int:
    cfg = fg.load_config()
    conn = fg.db(cfg)
    cursor = fg.state_get(conn, "cursor")
    if not cursor:
        cursor = str(time.time() - float(cfg["backfill_min"]) * 60)
        fg.state_set(conn, "cursor", cursor)
    conn.close()
    print(f"watchd start: frigate={cfg['url']} feed.enabled={cfg['feed']['enabled']} "
          f"level={cfg['feed']['level']} cursor={float(cursor):.0f}", flush=True)

    Poster(cfg).start()
    backoff = 0
    stale_since: float | None = None
    stale_alerted_episode: float | None = None

    def _sig(_s, _f):
        STOP.set()
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    while not STOP.is_set():
        cfg = fg.load_config()  # live tuning, cryptobro pattern
        api = fg.Frigate(cfg, timeout=15)
        conn = fg.db(cfg)
        cursor = float(fg.state_get(conn, "cursor", "0") or 0)
        overlap = float(cfg["overlap_s"])
        try:
            new_events: list[dict] = []
            for label in (cfg.get("labels") or ["person"]):
                cams = cfg.get("cameras") or []
                if cams:
                    for cam in cams:
                        new_events += api.events(after=cursor - overlap, label=label,
                                                 camera=cam, limit=500)
                else:
                    new_events += api.events(after=cursor - overlap, label=label, limit=500)
            if new_events:
                for raw in new_events:
                    upsert_event(conn, fg.norm_event(raw))
                cursor = max(cursor, max(float(e.get("start_time") or 0)
                                         for e in new_events))
                fg.state_set(conn, "cursor", str(int(cursor)))
            emitted = classify_and_emit(conn, api, cfg)
            if emitted:
                print(f"cycle: {len(new_events)} fetched, {emitted} envelopes enqueued",
                      flush=True)

            # staleness bookkeeping
            if stale_since is not None:
                _handle_recovery(conn, cfg, stale_since)
                stale_since = None
                stale_alerted_episode = None
            if stale_since is None:
                write_health(cfg, "running", None, cursor)
            backoff = 0
            conn.close()
        except fg.FrigateError as e:
            if stale_since is None:
                stale_since = time.time()
                print(f"frigate unreachable: {e}", flush=True)
            _handle_stale(conn, cfg, api, stale_since, stale_alerted_episode)
            write_health(cfg, "stale", stale_since, cursor, error=str(e))
            conn.close()
            backoff = min(backoff + 1, 5)
        try:
            prune(cfg)
        except Exception as e:
            print(f"prune error: {e}", flush=True)
        STOP.wait(float(cfg["poll_s"]) * (2 ** backoff if backoff else 1))
    print("watchd stop", flush=True)
    return 0


def _handle_stale(conn: sqlite3.Connection, cfg: dict, api: fg.Frigate,
                  stale_since: float, alerted: float | None) -> float | None:
    """After stale_action_min: one action envelope, then hourly info nudges."""
    now = time.time()
    mins = float(cfg["health"]["stale_action_min"])
    if not cfg["feed"]["enabled"] or now - stale_since < mins * 60:
        return alerted
    tz = fg.local_tz(cfg)
    bucket = datetime.now(tz).strftime("%Y%m%d%H")
    if alerted is None:
        envelope = {
            "source": "frigate", "type": "health.stale",
            "level": cfg["feed"]["level"],
            "dedup_key": f"frigate:stale:alert:{int(stale_since)}",
            "ttl_s": int(cfg["feed"]["ttl_action_s"]),
            "target_hint": "frigate",
            "summary": (f"frigate NVR unreachable for {int((now - stale_since) / 60)} min "
                        f"({cfg['url']}) — camera feed is blind until it recovers"),
            "body": {"url": cfg["url"], "stale_since": stale_since},
        }
        enqueue(conn, envelope)
        return stale_since
    if now - alerted >= float(cfg["health"]["stale_repeat_info_min"]) * 60:
        envelope = {
            "source": "frigate", "type": "health.stale",
            "level": "info",
            "dedup_key": f"frigate:stale:{bucket}",
            "ttl_s": int(cfg["feed"]["ttl_info_s"]),
            "target_hint": "frigate",
            "summary": (f"frigate still unreachable ({int((now - stale_since) / 60)} min)"),
            "body": {"url": cfg["url"], "stale_since": stale_since},
        }
        enqueue(conn, envelope)
        return now
    return alerted


def _handle_recovery(conn: sqlite3.Connection, cfg: dict, stale_since: float) -> None:
    now = time.time()
    mins = int((now - stale_since) / 60)
    print(f"frigate recovered after {mins} min", flush=True)
    if cfg["feed"]["enabled"] and mins >= float(cfg["health"]["stale_action_min"]):
        enqueue(conn, {
            "source": "frigate", "type": "health.recovered",
            "level": "info",
            "dedup_key": f"frigate:recovered:{int(stale_since)}",
            "ttl_s": int(cfg["feed"]["ttl_info_s"]),
            "target_hint": "frigate",
            "summary": f"frigate NVR back after {mins} min — feed resumed",
            "body": {"stale_since": stale_since, "recovered_at": now},
        })


if __name__ == "__main__":
    sys.exit(main())
