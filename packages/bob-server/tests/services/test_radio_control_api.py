"""Radio control API (the single-owner refactor, 2026-08-30): auth,
hardening caps, confirm gates, and the fetch-queue single-writer logic.
station.py / radio_domain.py live in the workspace skill, loaded by path.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

SKILL = Path("/home/bob/workspace/skills/radio")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def station_mod():
    return _load("radio_station_test", SKILL / "station.py")


@pytest.fixture(scope="module")
def domain_mod(station_mod):
    return sys.modules["radio_domain"]


class FakeStation:
    def __init__(self):
        self.source = "queue"

    def _status_dict(self):
        return {"now_playing": None, "source": self.source,
                "listeners": [], "station": "test"}

    def _render_now(self):
        return "ON AIR: (queue)\nqueue: 0 item(s)"


@pytest.fixture
def api(station_mod):
    return station_mod.ControlAPI(FakeStation(), "test-token")


def _dispatch(api, method, path, body=None, authorized=True):
    return asyncio.run(api.dispatch(method, path, body or {}, authorized))


def test_unauthorized_is_401(api):
    status, payload = _dispatch(api, "POST", "/skip", authorized=False)
    assert status == 401 and payload["ok"] is False
    assert "token" in payload["text"].lower()


def test_confirm_gates_on_destructive_ops(api, tmp_path, monkeypatch,
                                          domain_mod):
    # queue-clear and feature air refuse without confirm
    s, p = _dispatch(api, "POST", "/queue-clear", {"confirm": False})
    assert s == 400 and "confirm" in p["text"]
    s, p = _dispatch(api, "POST", "/feature",
                     {"sub": "air", "slug": "x", "confirm": False})
    assert s == 400 and "confirm" in p["text"]


def test_announce_length_cap(api):
    s, p = _dispatch(api, "POST", "/announce", {"text": "x" * 3000})
    assert s == 400 and "too long" in p["text"]


def test_announce_rate_limit(api, monkeypatch, domain_mod):
    monkeypatch.setattr(domain_mod, "queue_announcement",
                        lambda text, **k: (0, "queued"))
    for _ in range(api.ANNOUNCE_RATE_N):
        s, _p = _dispatch(api, "POST", "/announce", {"text": "hi"})
        assert s == 200
    s, p = _dispatch(api, "POST", "/announce", {"text": "hi"})
    assert s == 429 and "rate limit" in p["text"]


def test_fetch_rejects_non_spotify_uris(api):
    s, p = _dispatch(api, "POST", "/fetch", {"uris": ["-s", "search query"]})
    assert s == 400 and "spotify:" in p["text"]


def test_unknown_endpoint_404(api):
    s, _p = _dispatch(api, "GET", "/definitely-not")
    assert s == 404


def test_token_file_is_regenerated_not_static(station_mod):
    """The token must be generated per start (secrets), not a fixed value."""
    tok = station_mod._new_api_token()
    tok2 = station_mod._new_api_token()
    assert tok != tok2 and len(tok) >= 32
    f = station_mod.API_TOKEN_FILE
    assert f.read_text().strip() == tok2
    assert (f.stat().st_mode & 0o777) == 0o600


# ---------------- fetch queue: single-writer logic ---------------- #

@pytest.fixture
def jobfile(domain_mod, tmp_path, monkeypatch):
    jf = tmp_path / "fetch_jobs.json"
    monkeypatch.setattr(domain_mod, "FETCH_JOBS_FILE", jf)
    return jf


def test_enqueue_dedup_and_positions(domain_mod, jobfile):
    r1 = domain_mod.fetch_enqueue(["spotify:track:a"], kind="request")
    r2 = domain_mod.fetch_enqueue(["spotify:track:b", "spotify:track:c"])
    r3 = domain_mod.fetch_enqueue(["spotify:track:a"])
    assert r1["position"] == 1 and r2["position"] == 2
    assert r3["dup_of"] == r1["job"]["id"]


def test_partial_dedup_enqueues_only_fresh(domain_mod, jobfile):
    domain_mod.fetch_enqueue(["spotify:track:a"])
    r = domain_mod.fetch_enqueue(["spotify:track:a", "spotify:track:z"])
    assert "job" in r and r["job"]["uris"] == ["spotify:track:z"]


def test_batch_cap_and_claiming(domain_mod, jobfile):
    for i in range(30):
        domain_mod.fetch_enqueue([f"spotify:track:{i}"])
    batch = domain_mod.fetch_take_batch()
    assert batch is not None
    assert sum(len(j["uris"]) for j in batch) <= domain_mod.FETCH_BATCH_CAP
    assert all(j["status"] == "running" for j in batch)


def test_requeue_stale_running(domain_mod, jobfile):
    domain_mod.fetch_enqueue(["spotify:track:a"])
    batch = domain_mod.fetch_take_batch()
    assert batch and batch[0]["status"] == "running"
    n = domain_mod.fetch_requeue_stale()
    assert n == 1
    jobs = json.loads(jobfile.read_text())
    assert jobs[0]["status"] == "queued"


def test_empty_queue_returns_none(domain_mod, jobfile):
    assert domain_mod.fetch_take_batch() is None


def test_fetch_batch_runs_zotify_and_stamps(domain_mod, jobfile,
                                            monkeypatch, tmp_path):
    calls = []

    def fake_zotify(uris):
        calls.append(list(uris))
        return True, [{"type": "song", "path": "x.mp3",
                       "title": "T", "artist": "A", "origin": "fetch"}]

    monkeypatch.setattr(domain_mod, "_zotify_run", fake_zotify)
    monkeypatch.setattr(domain_mod, "append_queue", lambda items: 5)
    monkeypatch.setattr(domain_mod, "_attach_pending_dedications",
                        lambda items, fetched_uris=None: items)
    monkeypatch.setattr(domain_mod, "_prune_library", lambda: None)
    r = domain_mod.fetch_enqueue(["spotify:track:a", "spotify:track:b"])
    batch = domain_mod.fetch_take_batch()
    ok, queued = domain_mod.fetch_run_batch(batch)
    assert ok and queued == 1
    assert calls == [["spotify:track:a", "spotify:track:b"]]  # ONE zotify run
    jobs = json.loads(jobfile.read_text())
    assert jobs[0]["status"] == "done" and jobs[0]["batch_tracks"] == 1
