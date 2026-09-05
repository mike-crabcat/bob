"""Offline test suite for the frigate skill — mock NVR + mock stimulus spine.

Run:  python3 -m unittest discover -s skills/frigate/tests  (from workspace root)

The mock server records every request it serves; the suite asserts GET-only
across the board. Daemon logic is driven through the real functions
(upsert_event / classify_and_emit / Poster._drain) against a persistent
state.db, so crash-restart semantics are exercised for real.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))
import frigate as fg  # noqa: E402
import watchd  # noqa: E402

WORKSPACE = Path("/home/bob/workspace")
FFMPEG = fg.FFMPEG
HAS_FFMPEG = Path(FFMPEG).exists()

# a tiny real mp4, made once per suite (lavfi testsrc) — served as the clip
CLIP_BYTES = b""


def _make_clip() -> bytes:
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        path = f.name
    subprocess.run([
        FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
        "-i", "testsrc=duration=3:size=320x240:rate=10",
        "-pix_fmt", "yuv420p", path,
    ], check=True, capture_output=True)
    data = Path(path).read_bytes()
    Path(path).unlink(missing_ok=True)
    return data


def frigate_event(eid: str, camera="front_door", label="person",
                  start=None, end=None, zones=(), has_clip=True) -> dict:
    """A Frigate 0.14-shaped event row (score nested under `data`)."""
    start = start if start is not None else time.time() - 30
    return {
        "id": eid, "camera": camera, "label": label, "sub_label": None,
        "start_time": start, "end_time": end,
        "zones": list(zones), "entered_zones": list(zones),
        "score": None, "top_score": None,
        "has_clip": has_clip, "has_snapshot": True, "false_positive": None,
        "retain_indefinitely": False, "plus_id": None,
        "data": {"score": 0.6, "top_score": 0.87, "type": "object",
                 "attributes": [], "box": None, "region": None},
        "thumbnail": "", "box": None,
    }


class MockFrigateHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _record(self):
        self.server.requests.append((self.command, self.path))

    def do_GET(self):
        self._record()
        srv = self.server
        if self.path.startswith("/api/version"):
            self._text("0.14.1-mock")
        elif self.path.startswith("/api/config"):
            self._json({"cameras": {
                "front_door": {"zones": {"doorstep": {}}, "enabled": True,
                               "detect": {"enabled": True}},
                "garage": {"zones": {}, "enabled": True,
                           "detect": {"enabled": True}}}})
        elif "/snapshot.jpg" in self.path or "/latest.jpg" in self.path:
            self._bytes(srv.snapshot_jpg, "image/jpeg")
        elif "/clip.mp4" in self.path:
            eid = self.path.split("/")[3]
            if not srv.clips.get(eid):
                self.send_response(404); self.end_headers(); return
            self._bytes(srv.clips[eid], "video/mp4")
        elif self.path.startswith("/api/events"):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            after = float(q.get("after", [0])[0])
            before = q.get("before", [None])[0]
            label = q.get("label", [None])[0]
            camera = q.get("camera", [None])[0]
            limit = int(q.get("limit", [100])[0])
            out = [e for e in srv.events
                   if (e["start_time"] or 0) > after
                   and (before is None or (e["start_time"] or 0) < float(before))
                   and (label is None or e["label"] == label)
                   and (camera is None or e["camera"] == camera)]
            out = sorted(out, key=lambda e: e["start_time"], reverse=True)[:limit]
            self._json([dict(e, thumbnail=None) for e in out])
        else:
            self.send_response(404); self.end_headers()

    def _text(self, s):
        body = s.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        self._text(json.dumps(obj))

    def _bytes(self, data, mime):
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def srv_b64(user, password):
    import base64
    return base64.b64encode(f"{user}:{password}".encode()).decode()


class MockSpineHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.server.requests.append((self.command, self.path))
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        self.server.payloads.append(payload)
        self.server.auths.append(self.headers.get("Authorization", ""))
        code = self.server.response_code
        body = json.dumps({"id": len(self.server.payloads)}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Harness(unittest.TestCase):
    """Tmp state + mock NVR + mock spine + real skill code."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="frigate-test-"))
        self.state_db = self.tmp / "state.db"
        self.cache = self.tmp / "cache"
        self.config = self.tmp / "config.toml"

        self.nvr = ThreadingHTTPServer(("127.0.0.1", 0), MockFrigateHandler)
        self.nvr.requests = []
        self.nvr.events = []
        self.nvr.clips = {}
        self.nvr.snapshot_jpg = b"\xff\xd8fakejpg"
        self.nvr.require_auth = False
        self.spine = ThreadingHTTPServer(("127.0.0.1", 0), MockSpineHandler)
        self.spine.requests = []
        self.spine.payloads = []
        self.spine.auths = []
        self.spine.response_code = 201
        import threading
        for srv in (self.nvr, self.spine):
            threading.Thread(target=srv.serve_forever, daemon=True).start()

        self.write_config()
        self.env = {
            "FRIGATE_CONFIG": str(self.config),
            "FRIGATE_STATE_DB": str(self.state_db),
            "FRIGATE_CACHE_DIR": str(self.cache),
            "FRIGATE_URL": f"http://127.0.0.1:{self.nvr.server_address[1]}",
            "BOB_API_BASE": f"http://127.0.0.1:{self.spine.server_address[1]}",
            "BOB_STIMULUS_TOKEN": "test-token",
        }
        self._env_patch = mock.patch.dict(os.environ, self.env)
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self.nvr.shutdown(); self.nvr.server_close()
        self.spine.shutdown(); self.spine.server_close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        # GET-only, suite-wide: every request the mock NVR ever served
        for method, _path in self.nvr.requests:
            self.assertEqual(method, "GET",
                             f"non-GET request served: {method} {_path}")

    def write_config(self, **over):
        nvr_port = self.nvr.server_address[1]
        cfg = {
            "url": f"http://127.0.0.1:{nvr_port}",
            "poll_s": 1, "labels": ["person"], "cameras": [],
            "timezone": "Australia/Perth",
            "backfill_min": 15, "max_backfill_s": 86400, "overlap_s": 300,
            "feed": {"enabled": True, "level": "action",
                     "ttl_action_s": 900, "ttl_info_s": 600},
            "rules": {"action_zones": [], "action_hours": "00:00-23:59",
                      "cooldown_min": 10, "min_duration_s": 2.0,
                      "action_budget_per_hour": 6},
            "cache": {"snapshot_prune_days": 7, "clip_prune_days": 2,
                      "max_clip_mb": 200},
            "health": {"stale_action_min": 30, "stale_repeat_info_min": 60},
            "watch": {"out_width": 854, "out_fps": 5, "max_s": 10,
                      "crf": 30, "keyframes": 2, "sheet_tile_px": 480},
        }
        cfg.update(over)
        lines = [f'url = "{cfg["url"]}"', f'poll_s = {cfg["poll_s"]}',
                 'labels = ["person"]', "cameras = []",
                 'timezone = "Australia/Perth"',
                 f"backfill_min = {cfg['backfill_min']}",
                 f"max_backfill_s = {cfg['max_backfill_s']}",
                 f"overlap_s = {cfg['overlap_s']}"]
        for section in ("feed", "rules", "cache", "health", "watch"):
            lines.append(f"\n[{section}]")
            for k, v in cfg[section].items():
                if isinstance(v, bool):
                    lines.append(f"{k} = {'true' if v else 'false'}")
                elif isinstance(v, str):
                    lines.append(f'{k} = "{v}"')
                elif isinstance(v, list):
                    lines.append(f"{k} = {json.dumps(v)}")
                else:
                    lines.append(f"{k} = {v}")
        self.config.write_text("\n".join(lines) + "\n")

    # ── helpers ──

    def cfg(self) -> dict:
        return fg.load_config()

    def api(self) -> fg.Frigate:
        return fg.Frigate(self.cfg())

    def cycle(self, events=None):
        """One daemon poll+classify pass over the mock NVR, using the real
        watchd functions and the persistent state.db."""
        if events is not None:
            self.nvr.events = events
        cfg = self.cfg()
        api = fg.Frigate(cfg)
        conn = fg.db(cfg)
        cursor = float(fg.state_get(conn, "cursor", "0") or 0)
        fetched = []
        for label in (cfg.get("labels") or ["person"]):
            fetched += api.events(after=cursor - float(cfg["overlap_s"]),
                                  label=label, limit=500)
        for raw in fetched:
            watchd.upsert_event(conn, fg.norm_event(raw))
        if fetched:
            fg.state_set(conn, "cursor", str(int(max(
                cursor, max(float(e.get("start_time") or 0) for e in fetched)))))
        emitted = watchd.classify_and_emit(conn, api, cfg)
        conn.close()
        return emitted

    def outbox_pending(self):
        conn = fg.db(self.cfg())
        rows = conn.execute(
            "SELECT payload_json FROM outbox WHERE status='pending' "
            "ORDER BY id").fetchall()
        conn.close()
        return [json.loads(r["payload_json"]) for r in rows]

    def drain_poster(self):
        poster = watchd.Poster(self.cfg())
        return poster._drain()

    def run_cli(self, *args, env_extra=None):
        env = dict(os.environ)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable, str(SKILL_DIR / "frigate.py"), *args],
            capture_output=True, text=True, timeout=120, env=env, cwd=WORKSPACE)

    # ── lifecycle ──

    def test_lifecycle_open_blip_close(self):
        now = time.time()
        e = frigate_event("e1", start=now - 1, end=None)  # too fresh to judge
        self.assertEqual(self.cycle([e]), 0)
        self.assertEqual(self.outbox_pending(), [])

        # a sighting old enough to clear the blip filter → action envelope
        e2 = frigate_event("e2", start=now - 40, end=None)
        self.assertEqual(self.cycle([e, e2]), 1)
        (envelope,) = self.outbox_pending()
        self.assertEqual(envelope["dedup_key"], "frigate:e2")
        self.assertEqual(envelope["type"], "activity.person")
        self.assertEqual(envelope["level"], "action")
        snap_in_summary = envelope["summary"].split("snap=")[-1]
        self.assertTrue(Path(snap_in_summary).exists(),
                        f"summary snap path missing: {envelope['summary']}")
        # snapshot bytes actually cached
        snaps = list((self.cache / "snapshots").rglob("*.jpg"))
        self.assertEqual(len(snaps), 1)

        # close → info record, once
        self.nvr.events[1]["end_time"] = now - 5
        self.assertEqual(self.cycle(), 1)
        close = self.outbox_pending()[1]
        self.assertEqual(close["level"], "info")
        self.assertEqual(close["dedup_key"], "frigate:e2:close")
        self.assertEqual(close["body"]["emission"], "close")

        # re-poll: nothing new
        self.assertEqual(self.cycle(), 0)
        self.assertEqual(len(self.outbox_pending()), 2)

    def test_cooldown_suppresses_same_camera_only(self):
        now = time.time()
        a = frigate_event("a", camera="front_door", start=now - 40)
        b = frigate_event("b", camera="garage", start=now - 35)
        self.cycle([a, b])
        levels = {p["dedup_key"]: p["level"] for p in self.outbox_pending()}
        self.assertEqual(levels, {"frigate:a": "action", "frigate:b": "action"})

        # another event on a cooled-down camera: sighting suppressed entirely
        c = frigate_event("c", camera="front_door", start=now - 30, end=now - 10)
        self.cycle([a, b, c])
        dedups = {p["dedup_key"] for p in self.outbox_pending()}
        self.assertNotIn("frigate:c", dedups)          # no action
        self.assertIn("frigate:c:close", dedups)       # close record still lands

    def test_budget_downgrades_to_info(self):
        now = time.time()
        conn = fg.db(self.cfg())
        from datetime import datetime as dt
        hour = dt.now(fg.local_tz(self.cfg())).strftime("%Y%m%d%H")
        fg.state_set(conn, f"budget:{hour}", 6)  # budget exhausted
        conn.close()
        e = frigate_event("budget1", start=now - 40)
        self.cycle([e])
        (p,) = [p for p in self.outbox_pending() if p["dedup_key"] == "frigate:budget1"]
        self.assertEqual(p["level"], "info")
        self.assertEqual(p["body"]["downgraded"], "budget")

    def test_hours_window_blocks(self):
        now = time.time()
        self.write_config()  # base
        # narrow the window to one that excludes now: find a closed window
        from datetime import datetime as dt
        local = dt.now(fg.local_tz(self.cfg()))
        start_h = (local.hour + 2) % 24
        end_h = (local.hour + 3) % 24
        self.write_config()
        cfg = self.cfg()
        cfg["rules"]["action_hours"] = f"{start_h:02d}:00-{end_h:02d}:00"
        e = frigate_event("night1", start=now - 40)
        conn = fg.db(cfg)
        for raw in [e]:
            watchd.upsert_event(conn, fg.norm_event(raw))
        watchd.classify_and_emit(conn, fg.Frigate(cfg), cfg)
        conn.close()
        dedups = {p["dedup_key"] for p in self.outbox_pending()}
        self.assertNotIn("frigate:night1", dedups)

    def test_camera_allowlist(self):
        now = time.time()
        self.write_config()
        cfg = self.cfg()
        cfg["cameras"] = ["garage"]
        e = frigate_event("filtered", camera="front_door", start=now - 40)
        conn = fg.db(cfg)
        watchd.upsert_event(conn, fg.norm_event(e))
        watchd.classify_and_emit(conn, fg.Frigate(cfg), cfg)
        conn.close()
        self.assertEqual(self.outbox_pending(), [])

    # ── poster / outbox persistence ──

    def test_outbox_survives_spine_outage_and_no_double_send(self):
        now = time.time()
        e = frigate_event("persist1", start=now - 40, end=now - 5)
        self.cycle([e])
        self.assertEqual(len(self.outbox_pending()), 2)

        # spine down: point the poster at a dead port
        poster = watchd.Poster.__new__(watchd.Poster)
        poster.base = "http://127.0.0.1:9"
        poster.token = "test-token"
        poster.cfg = self.cfg()
        poster._drain()
        self.assertEqual(len(self.outbox_pending()), 2)  # still queued

        # spine back: drain until empty, then drain again — exactly 2 posts
        conn = fg.db(self.cfg())
        conn.execute("UPDATE outbox SET next_try=0, attempts=0")
        conn.commit(); conn.close()
        self.drain_poster()
        self.drain_poster()
        self.drain_poster()
        self.assertEqual(self.outbox_pending(), [])
        self.assertEqual(len(self.spine.payloads), 2)
        self.assertTrue(all(a == "Bearer test-token" for a in self.spine.auths))
        # re-drain on the same db: no double-send
        self.assertEqual(self.drain_poster(), 0)

    def test_poster_drops_4xx(self):
        now = time.time()
        self.spine.response_code = 400
        e = frigate_event("bad1", start=now - 40)
        self.cycle([e])
        self.drain_poster()
        conn = fg.db(self.cfg())
        statuses = [r["status"] for r in conn.execute(
            "SELECT status FROM outbox ORDER BY id").fetchall()]
        conn.close()
        self.assertIn("dropped", statuses)
        self.assertEqual(len(self.spine.payloads), 1)  # it was posted once

    def test_stale_envelope_once_then_hourly(self):
        cfg = self.cfg()
        conn = fg.db(cfg)
        stale_since = time.time() - 3600  # an hour stale
        alerted = watchd._handle_stale(conn, cfg, None, stale_since, None)
        self.assertEqual(alerted, stale_since)
        (p,) = self.outbox_pending()
        self.assertEqual(p["type"], "health.stale")
        self.assertEqual(p["level"], "action")
        self.assertIn("unreachable", p["summary"])
        # just alerted: no repeat within the window
        just = time.time()
        self.assertEqual(
            watchd._handle_stale(conn, cfg, None, stale_since, just), just)
        self.assertEqual(len(self.outbox_pending()), 1)
        # an hour after the alert: hourly info nudge
        alerted2 = watchd._handle_stale(
            conn, cfg, None, stale_since, time.time() - 3601)
        self.assertGreater(alerted2, 0)
        self.assertEqual(self.outbox_pending()[-1]["level"], "info")

    # ── auth ──

    def test_basic_auth_header_sent_and_secret_stays_out_of_output(self):
        with mock.patch.dict(os.environ, {
                "FRIGATE_USER": "alice", "FRIGATE_PASS": "s3cret-hunter2"}):
            api = fg.Frigate(self.cfg())
            api.events(limit=1)
        authed = [p for (m, p) in self.nvr.requests if "api/events" in p]
        self.assertTrue(authed)  # got served (mock doesn't enforce, header tested below)
        # direct header check
        with mock.patch.dict(os.environ, {
                "FRIGATE_USER": "alice", "FRIGATE_PASS": "s3cret-hunter2"}):
            api = fg.Frigate(self.cfg())
            req = urllib.request.Request(api.base + "/api/version")
            # replicate the client's header application
            import base64
            token = base64.b64encode(b"alice:s3cret-hunter2").decode()
            self.assertEqual(api._auth_header, f"Basic {token}")
        # errors never leak the secret
        with mock.patch.dict(os.environ, {"FRIGATE_URL": "http://127.0.0.1:9"}):
            r = self.run_cli("cameras")
        self.assertNotIn("s3cret-hunter2", r.stdout + r.stderr)

    # ── CLI surface ──

    def test_cli_cameras_and_events(self):
        now = time.time()
        self.nvr.events = [frigate_event("cli1", start=now - 60, end=now - 20)]
        r = self.run_cli("cameras")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("front_door", r.stdout)
        self.assertIn("0.14.1-mock", r.stdout)
        r = self.run_cli("events", "--since", "1h", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = json.loads(r.stdout)
        self.assertEqual(rows[0]["event_id"], "cli1")
        self.assertAlmostEqual(rows[0]["top_score"], 0.87)

    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg not available")
    def test_cli_watch_transcode_and_stills_and_gone(self):
        global CLIP_BYTES
        if not CLIP_BYTES:
            CLIP_BYTES = _make_clip()
        now = time.time()
        self.nvr.events = [frigate_event("w1", start=now - 60, end=now - 20)]
        self.nvr.clips["w1"] = CLIP_BYTES
        r = self.run_cli("watch", "w1")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        watch_clip = self.cache / "frames" / "w1-watch.mp4"
        self.assertTrue(watch_clip.exists())
        self.assertLess(watch_clip.stat().st_size, len(CLIP_BYTES))
        self.assertIn("read_video", r.stdout)
        # duration clamp honored (max_s=10 > 3s clip → stays ~3s)
        probe = json.loads(subprocess.run(
            [fg.FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format",
             str(watch_clip)], capture_output=True, text=True).stdout)
        self.assertLessEqual(float(probe["format"]["duration"]), 3.5)

        r = self.run_cli("watch", "w1", "--stills")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.cache / "frames" / "w1-sheet.jpg").exists())
        self.assertTrue((self.cache / "frames" / "w1-k1.jpg").exists())
        self.assertTrue((self.cache / "frames" / "w1-k2.jpg").exists())

        # unknown id → CLIP_GONE path
        r = self.run_cli("watch", "ghost")
        self.assertEqual(r.returncode, 1)
        self.assertIn("CLIP_GONE", r.stdout)

    def test_cli_watch_rejects_oversized_clip(self):
        global CLIP_BYTES
        if not HAS_FFMPEG:
            self.skipTest("ffmpeg not available")
        if not CLIP_BYTES:
            CLIP_BYTES = _make_clip()
        self.write_config()
        # shrink the cap to force the refusal
        text = self.config.read_text().replace("max_clip_mb = 200", "max_clip_mb = 0.000001")
        self.config.write_text(text)
        now = time.time()
        self.nvr.events = [frigate_event("big1", start=now - 60)]
        self.nvr.clips["big1"] = CLIP_BYTES
        r = self.run_cli("watch", "big1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("over the", r.stdout + r.stderr)

    def test_cli_snapshot_and_live(self):
        now = time.time()
        self.nvr.events = [frigate_event("s1", start=now - 60)]
        r = self.run_cli("snapshot", "s1")
        self.assertEqual(r.returncode, 0, r.stderr)
        snap = Path(r.stdout.strip().splitlines()[-1])
        self.assertTrue(snap.exists())
        r = self.run_cli("live", "front_door")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_cli_status(self):
        (SKILL_DIR / "run").mkdir(parents=True, exist_ok=True)
        r = self.run_cli("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("0.14.1-mock", r.stdout)

    def test_events_until_filters_interval(self):
        now = time.time()
        # three events: 2h ago, 30m ago, 5m ago — ask for the 40m..10m window
        self.nvr.events = [
            frigate_event("old", start=now - 7200, end=now - 7150),
            frigate_event("mid", start=now - 1800, end=now - 1750),
            frigate_event("new", start=now - 300, end=now - 250),
        ]
        r = self.run_cli("events", "--since", "40m", "--until", "10m", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        ids = [e["event_id"] for e in json.loads(r.stdout)]
        self.assertEqual(ids, ["mid"])

    def test_note_roundtrip_and_recap_merges(self):
        now = time.time()
        self.nvr.events = [
            frigate_event("n1", camera="front_door", start=now - 300, end=now - 250),
            frigate_event("n2", camera="garage", start=now - 200, end=now - 150),
        ]
        # populate state.db via a cycle, then note one event
        self.cycle()
        r = self.run_cli("note", "n1", "postie", "delivered", "a", "parcel")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("postie delivered a parcel", r.stdout)
        r = self.run_cli("recap", "--since", "1h")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("1 already watched+noted", r.stdout)
        self.assertIn("✓", r.stdout)
        self.assertIn("postie delivered a parcel", r.stdout)
        self.assertIn("n2" if "n2" not in r.stdout else "garage", r.stdout)
        # recap --json carries the note field
        r = self.run_cli("recap", "--since", "1h", "--json")
        rows = {e["event_id"]: e for e in json.loads(r.stdout)}
        self.assertEqual(rows["n1"]["note"], "postie delivered a parcel")
        self.assertIsNone(rows["n2"]["note"])

    def test_naive_iso_times_are_local_not_utc(self):
        cfg = self.cfg()
        tz = fg.local_tz(cfg)
        from datetime import datetime
        local_now = datetime.now(tz).strftime("%Y-%m-%dT%H:%M")
        ts = fg._parse_when(local_now, tz)
        self.assertLess(abs(ts - time.time()), 120)  # parsed as local now, not UTC

    def test_prune_respects_age(self):
        self.cache.joinpath("snapshots/202601").mkdir(parents=True, exist_ok=True)
        old = self.cache / "snapshots" / "202601" / "old.jpg"
        old.write_bytes(b"x")
        import os as _os
        _os.utime(old, (time.time() - 30 * 86400, time.time() - 30 * 86400))
        conn = fg.db(self.cfg())
        fg.state_set(conn, "last_prune", 0)
        conn.close()
        watchd.prune(self.cfg())
        self.assertFalse(old.exists())


if __name__ == "__main__":
    unittest.main()
